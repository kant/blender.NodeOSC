#    This Addon for Blender implements realtime OSC controls in the viewport
#
# ***** BEGIN GPL LICENSE BLOCK *****
#
#    Copyright (C) 2018  maybites <https://github.com/maybites/>
#
#    Copyright (C) 2017  AG6GR <https://github.com/AG6GR/>
#
#    Copyright (C) 2015  JPfeP <http://www.jpfep.net/>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# ***** END GPL LICENCE BLOCK *****

# TODO:
#
# attach the timer to the context window or not ?
# pbm not set to None du modal timer when opening a new blend file
# Bool are not part of OSC 1.0 (only later as extension)
# Deal with tupple (x,y,z) or (r,g,b) usr "type(key).__name__" for Vector, Euler, etc...
# Monitoring in console report error "Improper..." due to Monitoring refresh hack overhead


bl_info = {
    "name": "NodeOSC",
    "author": "maybites",
    "version": (0, 19),
    "blender": (2, 80, 0),
    "location": "View3D > Tools > NodeOSC",
    "description": "Realtime control of Blender using OSC protocol",
    "warning": "Please read the disclaimer about network security on the download site.",
    "wiki_url": "",
    "tracker_url": "",
    "category": "System"}

import bpy
import sys
import json
from select import select
import socket
import errno
import mathutils
from math import radians
from bpy.props import *
from ast import literal_eval as make_tuple

import os
script_file = os.path.realpath(__file__)
directory = os.path.dirname(script_file)
if directory not in sys.path:
   sys.path.append(directory)

from pythonosc import osc_message_builder
from pythonosc import udp_client
from pythonosc import osc_bundle
from pythonosc import osc_message
from pythonosc import osc_packet
from pythonosc import dispatcher
from pythonosc import osc_server
import threading
import socketserver
from bpy.app.handlers import persistent

import queue

_report= ["",""] #This for reporting OS network errors

#######################################
#  OSC Receive Method                 #
#######################################

# the OSC-server should not directly modify blender data from its own thread.
# instead we need a queue to store the callbacks and execute them inside
# a blender timer thread

# define the queue to store the callbacks
OSC_callback_queue = queue.LifoQueue()

# the repeatfilter, together with lifo (last in - first out) will
# make sure only the last osc message received on a certain address
# will be applied. all older messages will be ignored.
queue_repeat_filter = {}

# define the method the timer thread is calling when it is appropriate
def execute_queued_OSC_callbacks():
    queue_repeat_filter.clear()
    # while there are callbacks stored inside the queue
    while not OSC_callback_queue.empty():
        items = OSC_callback_queue.get()
        address = items[1]
        # if the address has not been here before:
        if queue_repeat_filter.get(address, False) == False:
            func = items[0]
            args = items[1:]
            # execute them 
            func(*args)
        queue_repeat_filter[address] = True
    return 0

# called by the queue execution thread
def OSC_callback_unkown(address, args):
    if bpy.context.window_manager.nodeosc_monitor == True:
        bpy.context.window_manager.nodeosc_lastaddr = address
        bpy.context.window_manager.nodeosc_lastpayload = str(args)

# called by the queue execution thread
def OSC_callback_custom(address, obj, attr, attrIdx, oscArgs, oscIndex):
    try:
        obj[attr] = oscArgs[oscIndex]
    except:
        if bpy.context.window_manager.nodeosc_monitor == True:
            print ("Improper content received: "+ address + " " + str(oscArgs))

# called by the queue execution thread
def OSC_callback_property(address, obj, attr, attrIdx, oscArgs, oscIndex):
    try:
        getattr(obj,attr)[attrIdx] = oscArgs[oscIndex]
    except:
        if bpy.context.window_manager.nodeosc_monitor == True:
            print ("Improper property received:: "+address + " " + str(oscArgs))

# called by the queue execution thread
def OSC_callback_properties(address, obj, attr, attrIdx, oscArgs, oscIndex):
    try:
        if len(oscIndex) == 3:
            getattr(obj, attr)[:] = oscArgs[oscIndex[0]], oscArgs[oscIndex[1]], oscArgs[oscIndex[2]]
        if len(oscIndex) == 4:
            getattr(obj, attr)[:] = oscArgs[oscIndex[0]], oscArgs[oscIndex[1]], oscArgs[oscIndex[2]], oscArgs[oscIndex[3]]
    except:
        if bpy.context.window_manager.nodeosc_monitor == True:
            print ("Improper properties received: "+address + " " + str(oscArgs))

# method called by the pythonosc library in case of an unmapped message
def OSC_callback_pythonosc_undef(* args):
    address = args[0]
    OSC_callback_queue.put((OSC_callback_unkown, address, args[2:]))

# method called by the pythonosc library in case of a mapped message
def OSC_callback_pythonosc(* args):
    # the args structure:
    #    args[0] = osc address
    #    args[1] = custom data pakage (tuplet with 5 values)
    #    args[>1] = osc arguments
    address = args[0]
    mytype = args[1][0][0]      # callback type 
    obj = args[1][0][1]          # blender object name (i.e. bpy.data.objects['Cube'])
    attr = args[1][0][2]        # blender object ID (i.e. location)
    attrIdx = args[1][0][3]         # ID-index (not used)
    oscIndex = args[1][0][4]    # osc argument index to use (should be a tuplet, like (1,2,3))

    oscArgs = args[2:]

    if mytype == 1:
        OSC_callback_queue.put((OSC_callback_custom, address, obj, attr, attrIdx, oscArgs, oscIndex))
    elif mytype == 2:
        OSC_callback_queue.put((OSC_callback_property, address, obj, attr, attrIdx, oscArgs, oscIndex))
    elif mytype == 3:
        OSC_callback_queue.put((OSC_callback_properties, address, obj, attr, attrIdx, oscArgs, oscIndex))
 
# method called by the pyliblo library in case of a mapped message
def OSC_callback_pyliblo(path, args, types, src, data):
    # the args structure:
    address = path
    mytype = data[0]        # callback type 
    obj = data[1]           # blender object name (i.e. bpy.data.objects['Cube'])
    attr = data[2]          # blender object ID (i.e. location)
    attrIdx = data[3]       # ID-index (not used)
    oscIndex = data[4]      # osc argument index to use (should be a tuplet, like (1,2,3))

    if mytype == 0:
        OSC_callback_queue.put((OSC_callback_unkown, address, args, data))
    elif mytype == 1:
        OSC_callback_queue.put((OSC_callback_custom, address, obj, attr, attrIdx, args, oscIndex))
    elif mytype == 2:
        OSC_callback_queue.put((OSC_callback_property, address, obj, attr, attrIdx, args, oscIndex))
    elif mytype == 3:
        OSC_callback_queue.put((OSC_callback_properties, address, obj, attr, attrIdx, args, oscIndex))

#For saving/restoring settings in the blendfile
def upd_settings_sub(n):
    text_settings = None
    for text in bpy.data.texts:
        if text.name == '.nodeosc_settings':
            text_settings = text
    if text_settings == None:
        bpy.ops.text.new()
        text_settings = bpy.data.texts[-1]
        text_settings.name = '.nodeosc_settings'
        text_settings.write("\n\n\n\n\n\n")
    if n==0:
        text_settings.lines[0].body = str(int(bpy.context.window_manager.nodeosc_monitor))
    elif n==1:
        text_settings.lines[1].body = str(bpy.context.window_manager.nodeosc_port_in)
    elif n==2:
        text_settings.lines[2].body = str(bpy.context.window_manager.nodeosc_port_out)
    elif n==3:
        text_settings.lines[3].body = str(bpy.context.window_manager.nodeosc_rate)
    elif n==4:
        text_settings.lines[4].body = bpy.context.window_manager.nodeosc_udp_in
    elif n==5:
        text_settings.lines[5].body = bpy.context.window_manager.nodeosc_udp_out
    elif n==6:
        text_settings.lines[6].body = str(int(bpy.context.window_manager.nodeosc_autorun))

def upd_setting_0():
    upd_settings_sub(0)

def upd_setting_1():
    upd_settings_sub(1)

def upd_setting_2():
    upd_settings_sub(2)

def upd_setting_3():
    upd_settings_sub(3)

def upd_setting_4():
    upd_settings_sub(4)

def upd_setting_5():
    upd_settings_sub(5)

def upd_setting_6():
    upd_settings_sub(6)

def osc_export_config(scene):
    config_table = {}
    for osc_item in scene.OSC_keys:
        config_table[osc_item.address] = {
            "data_path" : osc_item.data_path,
            "id" : osc_item.id,
            "osc_type" : osc_item.osc_type,
            "osc_index" : osc_item.osc_index
        }

    return json.dumps(config_table)

#######################################
#  Export OSC Settings                #
#######################################

class OSC_OT_ItemDelete(bpy.types.Operator):
    """Delete the  OSC Item """
    bl_idname = "nodeosc.deleteitem"
    bl_label = "Delete"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    index: bpy.props.IntProperty(default=0)

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        #file = open(self.filepath, 'w')
        #file.write(osc_export_config(context.scene))
        return {'FINISHED'}

    def invoke(self, context, event):
        bpy.context.scene.OSC_keys.remove(self.index)

        #for item in bpy.context.scene.OSC_keys:
        #    if item.idx == self.index:
        #        print(bpy.context.scene.OSC_keys.find(item))
        return {'RUNNING_MODAL'}

class OSC_Export(bpy.types.Operator):
    """Export the current OSC configuration to a file in JSON format"""
    bl_idname = "nodeosc.export"
    bl_label = "Export Config"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        file = open(self.filepath, 'w')
        file.write(osc_export_config(context.scene))
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

def osc_import_config(scene, config_file):
    config_table = json.load(config_file)
    for address, values in config_table.items():
        print(address)
        print(values)
        item = scene.OSC_keys.add()
        item.address = address
        item.data_path = values["data_path"]
        item.id = values["id"]
        item.osc_type = values["osc_type"]
        item.osc_index = values["osc_index"]

#######################################
#  Import OSC Settings                #
#######################################

class OSC_Import(bpy.types.Operator):
    """Import OSC configuration from a file in JSON format"""
    bl_idname = "nodeosc.import"
    bl_label = "Import Config"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        context.scene.OSC_keys.clear()
        config_file = open(self.filepath, 'r')
        osc_import_config(context.scene, config_file)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

#######################################
#  Setup OSC                          #
#######################################

class OSC_Reading_Sending(bpy.types.Operator):
    bl_idname = "nodeosc.modal_timer_operator"
    bl_label = "OSCMainThread"

    _timer = None
    client = "" #for the sending socket
    count = 0

    def upd_trick_nodeosc_monitor(self,context):
        upd_setting_0()

    def upd_trick_portin(self,context):
        upd_setting_1()

    def upd_trick_portout(self,context):
        upd_setting_2()

    def upd_trick_rate(self,context):
        upd_setting_3()

    def upd_trick_nodeosc_udp_in(self,context):
        upd_setting_4()

    def upd_trick_nodeosc_udp_out(self,context):
        upd_setting_5()

    def upd_trick_nodeosc_autorun(self,context):
        upd_setting_6()

    bpy.types.WindowManager.nodeosc_udp_in  = bpy.props.StringProperty(default="127.0.0.1", update=upd_trick_nodeosc_udp_in, description='The IP of the interface of your Blender machine to listen on, set to 0.0.0.0 for all of them')
    bpy.types.WindowManager.nodeosc_udp_out = bpy.props.StringProperty(default="127.0.0.1", update=upd_trick_nodeosc_udp_out, description='The IP of the destination machine to send messages to')
    bpy.types.WindowManager.nodeosc_port_in = bpy.props.IntProperty(default=9001, min=0, max=65535, update=upd_trick_portin, description='The input network port (0-65535)')
    bpy.types.WindowManager.nodeosc_port_out = bpy.props.IntProperty(default=9002, min=0, max= 65535, update=upd_trick_portout, description='The output network port (0-65535)')
    bpy.types.WindowManager.nodeosc_rate = bpy.props.IntProperty(default=10 ,description="The refresh rate of the engine (millisecond)", min=1, update=upd_trick_rate)
    bpy.types.WindowManager.status = bpy.props.StringProperty(default="Stopped", description='Show if the engine is running or not')
    bpy.types.WindowManager.nodeosc_monitor = bpy.props.BoolProperty(description="Display the current value of your keys, the last message received and some infos in console", update=upd_trick_nodeosc_monitor)
    bpy.types.WindowManager.nodeosc_autorun = bpy.props.BoolProperty(description="Start the OSC engine automatically after loading a project", update=upd_trick_nodeosc_autorun)
    bpy.types.WindowManager.nodeosc_lastaddr = bpy.props.StringProperty(description="Display the last OSC address received")
    bpy.types.WindowManager.nodeosc_lastpayload = bpy.props.StringProperty(description="Display the last OSC message content")

    #modes_enum = [('Replace','Replace','Replace'),('Update','Update','Update')]
    #bpy.types.WindowManager.nodeosc_mode = bpy.props.EnumProperty(name = "import mode", items = modes_enum)

    #######################################
    #  Sending OSC                        #
    #######################################

    def modal(self, context, event):
        if context.window_manager.status == "Stopped":
            return self.cancel(context)

        if event.type == 'TIMER':
            #hack to refresh the GUI
            bcw = bpy.context.window_manager
            self.count = self.count + bcw.nodeosc_rate
            if self.count >= 500:
                self.count = 0
                if bpy.context.window_manager.nodeosc_monitor == True:
                    for window in bpy.context.window_manager.windows:
                        screen = window.screen
                        for area in screen.areas:
                            if area.type == 'VIEW_3D':
                                area.tag_redraw()
            #Sending
            for item in bpy.context.scene.OSC_keys:
                #print( "sending  :{}".format(item) )
                if item.id[0:2] == '["' and item.id[-2:] == '"]':
                    prop = eval(item.data_path+item.id)
                else:
                    prop = eval(item.data_path+'.'+item.id)

                if isinstance(prop, mathutils.Vector):
                    prop = list(prop)

                if isinstance(prop, mathutils.Quaternion):
                    prop = list(prop)

                if str(prop) != item.value:
                    item.value = str(prop)

                    if item.idx == 0:
                        msg = osc_message_builder.OscMessageBuilder(address=item.address)
                        #print( "sending prop :{}".format(prop) )
                        if isinstance(prop, list):
                            for argmnts in prop:
                                msg.add_arg(argmnts)
                        else:
                            msg.add_arg(prop)
                        msg = msg.build()
                        self.client.send(msg)
        return {'PASS_THROUGH'}

    #######################################
    #  Setup OSC Receiver and Sender      #
    #######################################

    def execute(self, context):
        global _report
        bcw = bpy.context.window_manager

        #For sending
        try:
            self.client = udp_client.UDPClient(bcw.nodeosc_udp_out, bcw.nodeosc_port_out)
            msg = osc_message_builder.OscMessageBuilder(address="/blender")
            msg.add_arg("Hello from Blender, simple test.")
            msg = msg.build()
            self.client.send(msg)
        except OSError as err:
            _report[1] = err
            return {'CANCELLED'}
 
        #Setting up the dispatcher for receiving
        try:
            self.dispatcher = dispatcher.Dispatcher()            
            for item in bpy.context.scene.OSC_keys:

                #For ID custom properties (with brackets)
                if item.id[0:2] == '["' and item.id[-2:] == '"]':
                    dataTuple = (1, eval(item.data_path), item.id, item.idx, make_tuple(item.osc_index))
                    self.dispatcher.map(item.address, OSC_callback_pythonosc, dataTuple)
                #For normal properties
                #with index in brackets -: i_num
                elif item.id[-1] == ']':
                    d_p = item.id[:-3]
                    i_num = int(item.id[-2])
                    dataTuple = (2, eval(item.data_path), d_p, i_num, make_tuple(item.osc_index))
                    self.dispatcher.map(item.address, OSC_callback_pythonosc, dataTuple)
                #without index in brackets
                else:
                    try:
                        if isinstance(getattr(eval(item.data_path), item.id), mathutils.Vector):
                            dataTuple = (3, eval(item.data_path), item.id, item.idx, make_tuple(item.osc_index))
                            self.dispatcher.map(item.address, OSC_callback_pythonosc, dataTuple)
                        elif isinstance(getattr(eval(item.data_path), item.id), mathutils.Quaternion):
                            dataTuple = (3, eval(item.data_path), item.id, item.idx, make_tuple(item.osc_index))
                            self.dispatcher.map(item.address, OSC_callback_pythonosc, dataTuple)
                    except:
                        print ("Improper setup received: object '"+item.data_path+"' with id'"+item.id+"' is no recognized dataformat")
 
            self.dispatcher.set_default_handler(OSC_callback_pythonosc_undef)
 
            print("Create Server Thread on Port", bcw.nodeosc_port_in)
            # creating a blocking UDP Server
            #   Each message will be handled sequentially on the same thread.
            #   the alternative: 
            #       ThreadingOSCUDPServer creates loads of threads 
            #       that are not cleaned up properly
            self.server = osc_server.BlockingOSCUDPServer((bcw.nodeosc_udp_in, bcw.nodeosc_port_in), self.dispatcher)
            self.server_thread = threading.Thread(target=self.server.serve_forever)
            self.server_thread.start()
            # register the execute queue method
            bpy.app.timers.register(execute_queued_OSC_callbacks)

        except OSError as err:
            _report[0] = err
            return {'CANCELLED'}


        #inititate the modal timer thread
        context.window_manager.modal_handler_add(self)
        self._timer = context.window_manager.event_timer_add(bcw.nodeosc_rate/1000, window = context.window)
        context.window_manager.status = "Running"

        return {'RUNNING_MODAL'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        print("OSC server.shutdown()")
        self.server.shutdown()
        context.window_manager.status = "Stopped"
        bpy.app.timers.unregister(execute_queued_OSC_callbacks)
        return {'CANCELLED'}

#######################################
#  MAIN GUI PANEL                     #
#######################################

class OSC_PT_Settings(bpy.types.Panel):
    bl_category = "NodeOSC"
    bl_label = "NodeOSC Settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_context = "objectmode"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="OSC Settings:")
        row = col.row(align=True)
        row.operator("nodeosc.startudp", text='Start', icon='PLAY')
        row.operator("nodeosc.stopudp", text='Stop', icon='PAUSE')
        layout.prop(bpy.context.window_manager, 'status', text="Running Status")
        layout.prop(bpy.context.window_manager, 'nodeosc_udp_in', text="Listen on ")
        layout.prop(bpy.context.window_manager, 'nodeosc_udp_out', text="Destination address")
        col2 = layout.column(align=True)
        row2 = col2.row(align=True)
        row2.prop(bpy.context.window_manager, 'nodeosc_port_in', text="Input port")
        row2.prop(bpy.context.window_manager, 'nodeosc_port_out', text="Outport port")
        layout.prop(bpy.context.window_manager, 'nodeosc_rate', text="Update rate(ms)")
        layout.prop(bpy.context.window_manager, 'nodeosc_autorun', text="Start at Launch")
 
#######################################
#  OPERATIONS GUI PANEL               #
#######################################

class OSC_PT_Operations(bpy.types.Panel):
    bl_category = "NodeOSC"
    bl_label = "NodeOSC Operations"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_context = "objectmode"

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=False)
        row.prop(bpy.context.scene, 'nodeosc_defaultaddr', text="Default Address")
        row.prop(bpy.context.window_manager, 'nodeosc_monitor', text="Monitoring")

        if context.window_manager.nodeosc_monitor == True:
            box = layout.box()
            row5 = box.column(align=True)
            row5.prop(bpy.context.window_manager, 'nodeosc_lastaddr', text="Last OSC address")
            row5.prop(bpy.context.window_manager, 'nodeosc_lastpayload', text="Last OSC message")

        layout.separator()
        layout.operator("nodeosc.importks", text='Import Keying Set')
        row = layout.row(align=True)
        row.operator("nodeosc.export", text='Export OSC Config')
        row.operator("nodeosc.import", text='Import OSC Config')

        layout.separator()
        layout.label(text="Imported Keys:")
        index = 0
        for item in bpy.context.scene.OSC_keys:
            box3 = layout.box()
            #split = box3.split()
            rowItm1 = box3.row()
            if bpy.context.window_manager.nodeosc_monitor == True:
                rowItm1.operator("nodeosc.pick", text='', icon='EYEDROPPER').i_addr = item.address
            rowItm1.prop(item, 'address',text='Osc-addr')
            rowItm1.prop(item, 'osc_index',text='Osc-argument[index]')
            #rowItm1.label(text="("+item.osc_type+")")
             
            rowItm2 = box3.row()
            rowItm2.prop(item,'data_path',text='Blender-path')
            rowItm2.prop(item,'id',text='ID')
            rowItm2.operator("nodeosc.deleteitem", icon='CANCEL').index = index
            
            if bpy.context.window_manager.nodeosc_monitor == True:
                rowItm3 = box3.row()
                rowItm3.prop(item, 'value',text='current value')
            index = index + 1
                 

class StartUDP(bpy.types.Operator):
    bl_idname = "nodeosc.startudp"
    bl_label = "Start UDP Connection"
    bl_description ="Start the OSC engine"

    def execute(self, context):
        global _report
        if context.window_manager.nodeosc_port_in == context.window_manager.nodeosc_port_out:
            self.report({'INFO'}, "Ports must be different.")
            return{'FINISHED'}
        if bpy.context.window_manager.status != "Running" :
            bpy.ops.nodeosc.modal_timer_operator()
            if _report[0] != '':
                self.report({'INFO'}, "Input error: {0}".format(_report[0]))
                _report[0] = ''
            elif _report[1] != '':
                self.report({'INFO'}, "Output error: {0}".format(_report[1]))
                _report[1] = ''
        else:
            self.report({'INFO'}, "Already connected !")
        return{'FINISHED'}

class StopUDP(bpy.types.Operator):
    bl_idname = "nodeosc.stopudp"
    bl_label = "Stop UDP Connection"
    bl_description ="Stop the OSC engine"

    def execute(self, context):
        self.report({'INFO'}, "Disconnected !")
        bpy.context.window_manager.status = "Stopped"
        return{'FINISHED'}

class PickOSCaddress(bpy.types.Operator):
    bl_idname = "nodeosc.pick"
    bl_label = "Pick the last event OSC address"
    bl_options = {'UNDO'}
    bl_description ="Pick the address of the last OSC message received"

    i_addr: bpy.props.StringProperty()

    def execute(self, context):
        last_event = bpy.context.window_manager.nodeosc_lastaddr
        if len(last_event) > 1 and last_event[0] == "/":
            for item in bpy.context.scene.OSC_keys:
                if item.address == self.i_addr :
                    item.address = last_event
        return{'FINISHED'}



#Restore saved settings
@persistent
def nodeosc_handler(scene):
    for text in bpy.data.texts:
        if text.name == '.nodeosc_settings':
            try:
                bpy.context.window_manager.nodeosc_monitor = int(text.lines[0].body)
            except:
                pass
            try:
                bpy.context.window_manager.nodeosc_port_in  = int(text.lines[1].body)
            except:
                pass
            try:
                bpy.context.window_manager.nodeosc_port_out = int(text.lines[2].body)
            except:
                pass
            try:
                bpy.context.window_manager.nodeosc_rate = int(text.lines[3].body)
            except:
                bpy.context.window_manager.nodeosc_rate = 10
            if text.lines[4].body != '':
                bpy.context.window_manager.nodeosc_udp_in = text.lines[4].body
            if text.lines[5].body != '':
                bpy.context.window_manager.nodeosc_udp_out = text.lines[5].body
            try:
                bpy.context.window_manager.nodeosc_autorun = int(text.lines[6].body)
            except:
                pass

            #if error_device == True:
            #    bpy.context.window_manager.nodeosc_autorun = False

            if bpy.context.window_manager.nodeosc_autorun == True:
                bpy.ops.nodeosc.startudp()


classes = (
    OSC_Export,
    OSC_Import,
    OSC_Reading_Sending,
    OSC_PT_Settings,
    OSC_PT_Operations,
    StartUDP,
    StopUDP,
    PickOSCaddress,
    OSC_OT_ItemDelete
)

from .AN import auto_load
auto_load.init()

def register():
    from . import preferences
    preferences.register()
    from . import keys
    keys.register()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.load_post.append(nodeosc_handler)
    auto_load.register()

def unregister():
    auto_load.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    from . import keys
    keys.unregister()
    from . import preferences
    preferences.unregister()

if __name__ == "__main__":
    register()
