#!/usr/bin/env python2
# -*-coding:UTF-8 -*

from asciimatics.widgets import Frame, ListBox, Layout, Divider, Text, \
    Button, TextBox, Widget, Label
from asciimatics.effects import Cycle, Print, Stars
from asciimatics.scene import Scene
from asciimatics.screen import Screen
from asciimatics.exceptions import ResizeScreenError, NextScene, StopApplication
from asciimatics.event import Event
from asciimatics.event import KeyboardEvent, MouseEvent
import sys, os
import time, datetime
import argparse, ConfigParser
import json
import redis
import psutil
from subprocess import PIPE, Popen

 # CONFIG VARIABLES
kill_retry_threshold = 60 #1m
log_filename = "../logs/moduleInfo.log"
command_search_pid = "ps a -o pid,cmd | grep {}"
command_search_name = "ps a -o pid,cmd | grep {}"
command_restart_module = "screen -S \"Script\" -X screen -t \"{}\" bash -c \"./{}.py; read x\""

printarrayGlob = [None]*14
lastTimeKillCommand = {}

current_selected_value = 0
current_selected_queue = ""
current_selected_action = ""
current_selected_action = 0

PID_NAME_DICO = {}

TABLES = {"running": [], "idle": [], "notRunning": [], "logs": [("No events recorded yet", 0)]}
TABLES_TITLES = {"running": "", "idle": "", "notRunning": "", "logs": ""}
TABLES_PADDING = {"running": [12, 23, 8, 8, 23, 10, 55, 11, 11, 12], "idle": [9, 23, 8, 12, 50], "notRunning": [9, 23, 35], "logs": [15, 23, 8, 50]}

QUEUE_STATUS = {}
CPU_TABLE = {} 
CPU_OBJECT_TABLE = {}

class CListBox(ListBox):

    def __init__(self, queue_name, *args, **kwargs):
        self.queue_name = queue_name
        super(CListBox, self).__init__(*args, **kwargs)

    def update(self, frame_no):
        self._options = TABLES[self.queue_name]

        self._draw_label()

        # Calculate new visible limits if needed.
        width = self._w - self._offset
        height = self._h
        dx = dy = 0

        # Clear out the existing box content
        (colour, attr, bg) = self._frame.palette["field"]
        for i in range(height):
            self._frame.canvas.print_at(
                " " * width,
                self._x + self._offset + dx,
                self._y + i + dy,
                colour, attr, bg)

        # Don't bother with anything else if there are no options to render.
        if len(self._options) <= 0:
            return

        # Render visible portion of the text.
        self._start_line = max(0, max(self._line - height + 1,
                                      min(self._start_line, self._line)))
        for i, (text, pid) in enumerate(self._options):
            if self._start_line <= i < self._start_line + height:
                colour, attr, bg = self._pick_colours("field", i == self._line)
                self._frame.canvas.print_at(
                    "{:{width}}".format(text, width=width),
                    self._x + self._offset + dx,
                    self._y + i + dy - self._start_line,
                    colour, attr, bg)
                if self.queue_name == "running":
                    if QUEUE_STATUS[pid] == 2:
                        queueStatus = Screen.COLOUR_RED
                    elif QUEUE_STATUS[pid] == 1:
                        queueStatus = Screen.COLOUR_YELLOW
                    else:
                        queueStatus = Screen.COLOUR_GREEN

                    self._frame.canvas.print_at(" ",
                        self._x + 9 + dx,
                        self._y + i + dy - self._start_line,
                        colour, attr, queueStatus)


    def process_event(self, event):
        if isinstance(event, KeyboardEvent):
            if len(self._options) > 0 and event.key_code == Screen.KEY_UP:
                # Move up one line in text - use value to trigger on_select.
                self._line = max(0, self._line - 1)
                self.value = self._options[self._line][1]
            elif len(self._options) > 0 and event.key_code == Screen.KEY_DOWN:
                # Move down one line in text - use value to trigger on_select.
                self._line = min(len(self._options) - 1, self._line + 1)
                self.value = self._options[self._line][1]
            elif len(self._options) > 0 and event.key_code == ord(' '):
                global current_selected_value, current_selected_queue
                current_selected_value = self.value
                current_selected_queue = self.queue_name
                self._frame.save()
                raise NextScene("action_choice")
            else:
                # Ignore any other key press.
                return event
        elif isinstance(event, MouseEvent):
            # Mouse event - rebase coordinates to Frame context.
            new_event = self._frame.rebase_event(event)
            if event.buttons != 0:
                if (len(self._options) > 0 and
                        self.is_mouse_over(new_event, include_label=False)):
                    # Use property to trigger events.
                    self._line = min(new_event.y - self._y,
                                     len(self._options) - 1)
                    self.value = self._options[self._line][1]
                    # If clicked on button <k>, kill the queue
                    if self._x+2 <= new_event.x < self._x+4:
                        if self.queue_name in ["running", "idle"]:
                            kill_module(PID_NAME_DICO[int(self.value)], self.value)
                        else:
                            restart_module(self.value)

                    return
            # Ignore other mouse events.
            return event
        else:
            # Ignore other events
            return event


class CLabel(Label):
    def __init__(self, label, listTitle=False):
        super(Label, self).__init__(None, tab_stop=False)
        # Although this is a label, we don't want it to contribute to the layout
        # tab calculations, so leave internal `_label` value as None.
        self._text = label
        self.listTitle = listTitle

    def set_layout(self, x, y, offset, w, h):
        # Do the usual layout work. then recalculate exact x/w values for the
        # rendered button.
        super(Label, self).set_layout(x, y, offset, w, h)
        self._x += max(0, (self._w - self._offset - len(self._text)) // 2) if not self.listTitle else 0
        self._w = min(self._w, len(self._text))

    def update(self, frame_no):
        (colour, attr, bg) = self._frame.palette["title"]
        colour = Screen.COLOUR_YELLOW if not self.listTitle else colour
        self._frame.canvas.print_at(
            self._text, self._x, self._y, colour, attr, bg)

class ListView(Frame):
    def __init__(self, screen):
        super(ListView, self).__init__(screen,
                                       screen.height,
                                       screen.width,
                                       hover_focus=True,
                                       reduce_cpu=True)

        self._list_view_run_queue = CListBox(
            "running",
            screen.height // 2,
            [], name="LIST")
        self._list_view_idle_queue = CListBox(
            "idle",
            screen.height // 2,
            [], name="LIST")
        self._list_view_noRunning = CListBox(
            "notRunning",
            screen.height // 5,
            [], name="LIST")
        self._list_view_Log = CListBox(
            "logs",
            screen.height // 4,
            [], name="LIST")
        #self._list_view_Log.disabled = True


        #Running Queues
        layout = Layout([100])
        self.add_layout(layout)
        text_rq = CLabel("Running Queues")
        layout.add_widget(text_rq)
        layout.add_widget(CLabel(TABLES_TITLES["running"], listTitle=True))
        layout.add_widget(self._list_view_run_queue)
        layout.add_widget(Divider())

        #Idling Queues
        layout2 = Layout([1,1])
        self.add_layout(layout2)
        text_iq = CLabel("Idling Queues")
        layout2.add_widget(text_iq, 0)
        layout2.add_widget(CLabel(TABLES_TITLES["idle"], listTitle=True), 0)
        layout2.add_widget(self._list_view_idle_queue, 0)
        #Non Running Queues
        text_nq = CLabel("No Running Queues")
        layout2.add_widget(text_nq, 1)
        layout2.add_widget(CLabel(TABLES_TITLES["notRunning"], listTitle=True), 1)
        layout2.add_widget(self._list_view_noRunning, 1)
        layout2.add_widget(Divider(), 1)
        #Log
        text_l = CLabel("Logs")
        layout2.add_widget(text_l, 1)
        layout2.add_widget(CLabel(TABLES_TITLES["logs"], listTitle=True), 1)
        layout2.add_widget(self._list_view_Log, 1)

        self.fix()

    @staticmethod
    def _quit():
        raise StopApplication("User pressed quit")

class Confirm(Frame):
    def __init__(self, screen):
        super(Confirm, self).__init__(screen,
                                          screen.height * 1 // 8,
                                          screen.width * 1 // 4,
                                          hover_focus=True,
                                          on_load=self._setValue,
                                          title="Confirm action",
                                          reduce_cpu=True)

        # Create the form for displaying the list of contacts.
        layout = Layout([100], fill_frame=True)
        self.add_layout(layout)
        self.label = CLabel("{} module {} {}?")
        layout.add_widget(Label(" "))
        layout.add_widget(self.label)
        layout2 = Layout([1,1])
        self.add_layout(layout2)
        layout2.add_widget(Button("Ok", self._ok), 0)
        layout2.add_widget(Button("Cancel", self._cancel), 1)

        self.fix()

    def _ok(self):
        global current_selected_value, current_selected_queue, current_selected_action, current_selected_amount
        if current_selected_action == "KILL":
            kill_module(PID_NAME_DICO[int(current_selected_value)], current_selected_value)
        else:
            count = int(current_selected_amount)
            if current_selected_queue in ["running", "idle"]:
                restart_module(PID_NAME_DICO[int(current_selected_value)], count)
            else:
                restart_module(current_selected_value, count)

        current_selected_value = 0
        current_selected_amount = 0
        current_selected_action = ""
        self.label._text = "{} module {} {}?"
        self.save()
        raise NextScene("dashboard")

    def _cancel(self):
        global current_selected_value
        current_selected_value = 0
        current_selected_amount = 0
        current_selected_action = ""
        self.label._text = "{} module {} {}?"
        self.save()
        raise NextScene("dashboard")

    def _setValue(self):
        global current_selected_value, current_selected_queue, current_selected_action, current_selected_amount
        if current_selected_queue in ["running", "idle"]:
            action = current_selected_action if current_selected_action == "KILL" else current_selected_action +" "+ str(current_selected_amount) + "x"
            modulename = PID_NAME_DICO[int(current_selected_value)]
            pid = current_selected_value
        else:
            action = current_selected_action + " " + str(current_selected_amount) + "x"
            modulename = current_selected_value
            pid = ""
        self.label._text = self.label._text.format(action, modulename, pid)

class Action_choice(Frame):
    def __init__(self, screen):
        super(Action_choice, self).__init__(screen,
                                          screen.height * 1 // 8,
                                          screen.width * 1 // 2,
                                          hover_focus=True,
                                          on_load=self._setValue,
                                          title="Confirm action",
                                          reduce_cpu=True)

        # Create the form for displaying the list of contacts.
        layout = Layout([100], fill_frame=True)
        self.add_layout(layout)
        self.label = CLabel("Choose action on module {} {}")
        layout.add_widget(self.label)
        layout2 = Layout([1,1,1])
        self.add_layout(layout2)
        layout2.add_widget(Button("Cancel", self._cancel), 0)
        self._killBtn = Button("KILL", self._kill)
        layout2.add_widget(self._killBtn, 1)
        layout2.add_widget(Button("START", self._start), 2)
        layout3 = Layout([1,1,1])
        self.add_layout(layout3)
        self.textEdit = Text("Amount", "amount")
        layout3.add_widget(self.textEdit, 2)

        self.fix()

    def _kill(self):
        global current_selected_action
        current_selected_action = "KILL"
        self.label._text = "Choose action on module {} {}"
        self.save()
        raise NextScene("confirm")

    def _start(self):
        global current_selected_action, current_selected_amount
        current_selected_action = "START"
        try:
            count = int(self.textEdit.value)
            count = count if count < 20 else 1
        except Exception:
            count = 1
        current_selected_amount = count
        self.label._text = "Choose action on module {} {}"
        self.save()
        raise NextScene("confirm")


    def _cancel(self):
        global current_selected_value
        current_selected_value = 0
        self.label._text = "Choose action on module {} {}"
        self.save()
        raise NextScene("dashboard")

    def _setValue(self):
        self._killBtn.disabled = False
        global current_selected_value, current_selected_queue
        if current_selected_queue in ["running", "idle"]:
            modulename = PID_NAME_DICO[int(current_selected_value)]
            pid = current_selected_value
        else:
            self._killBtn.disabled = True
            modulename = current_selected_value
            pid = ""
        self.label._text = self.label._text.format(modulename, pid)

def demo(screen):
    dashboard = ListView(screen)
    confirm = Confirm(screen)
    action_choice = Action_choice(screen)
    scenes = [
        Scene([dashboard], -1, name="dashboard"),
        Scene([action_choice], -1, name="action_choice"),
        Scene([confirm], -1, name="confirm"),
    ]

   # screen.play(scenes)
    screen.set_scenes(scenes)
    time_cooldown = time.time()
    global TABLES
    while True:
        if time.time() - time_cooldown > args.refresh:
            cleanRedis()
            for key, val in fetchQueueData().iteritems():
                TABLES[key] = val
            TABLES["logs"] = format_string(printarrayGlob, TABLES_PADDING["logs"])
            if current_selected_value == 0:
                dashboard._update(None)
                screen.refresh()
            time_cooldown = time.time()
        screen.draw_next_frame()
        time.sleep(0.02)


def getPid(module):
    p = Popen([command_search_pid.format(module+".py")], stdin=PIPE, stdout=PIPE, bufsize=1, shell=True)
    for line in p.stdout:
        print line
        splittedLine = line.split()
        if 'python2' in splittedLine:
            return int(splittedLine[0])
    return None

def clearRedisModuleInfo():
    for k in server.keys("MODULE_*"):
        server.delete(k)
    inst_time = datetime.datetime.fromtimestamp(int(time.time()))
    printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], "*", "-", "Cleared redis module info"], 0))
    printarrayGlob.pop()

def cleanRedis():
    for k in server.keys("MODULE_TYPE_*"):
        moduleName = k[12:].split('_')[0]
        for pid in server.smembers(k):
            flag_pid_valid = False
            proc = Popen([command_search_name.format(pid)], stdin=PIPE, stdout=PIPE, bufsize=1, shell=True)
            for line in proc.stdout:
                splittedLine = line.split()
                if ('python2' in splittedLine or 'python' in splittedLine) and "./"+moduleName+".py" in splittedLine:
                    flag_pid_valid = True

            if not flag_pid_valid:
                #print flag_pid_valid, 'cleaning', pid, 'in', k
                server.srem(k, pid)
                inst_time = datetime.datetime.fromtimestamp(int(time.time()))
                printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], moduleName, pid, "Cleared invalid pid in " + k], 0))
                printarrayGlob.pop()
                #time.sleep(5)

def restart_module(module, count=1):
    for i in range(count):
        p2 = Popen([command_restart_module.format(module, module)], stdin=PIPE, stdout=PIPE, bufsize=1, shell=True)
        time.sleep(0.2)
    inst_time = datetime.datetime.fromtimestamp(int(time.time()))
    printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, "?", "Restarted " + str(count) + "x"], 0))
    printarrayGlob.pop()



def kill_module(module, pid):
    #print ''
    #print '-> trying to kill module:', module

    if pid is None:
        #print 'pid was None'
        inst_time = datetime.datetime.fromtimestamp(int(time.time()))
        printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "PID was None"], 0))
        printarrayGlob.pop()
        pid = getPid(module)
    else: #Verify that the pid is at least in redis
        if server.exists("MODULE_"+module+"_"+str(pid)) == 0:
            return

    lastTimeKillCommand[pid] = int(time.time())
    if pid is not None:
        try:
            #os.kill(pid, signal.SIGUSR1)
            p = psutil.Process(int(pid))
            p.terminate()
        except Exception as e:
            #print pid, 'already killed'
            inst_time = datetime.datetime.fromtimestamp(int(time.time()))
            printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "Already killed"], 0))
            printarrayGlob.pop()
            return
        time.sleep(0.2)
        if not p.is_running():
            #print module, 'has been killed'
            #print 'restarting', module, '...'
            inst_time = datetime.datetime.fromtimestamp(int(time.time()))
            printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "Killed"], 0))
            printarrayGlob.pop()
            #restart_module(module)

        else:
            #print 'killing failed, retrying...'
            inst_time = datetime.datetime.fromtimestamp(int(time.time()))
            printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "Killing #1 failed."], 0))
            printarrayGlob.pop()

            #os.kill(pid, signal.SIGUSR1)
            #time.sleep(1)
            p.terminate()
            if not p.is_running():
                #print module, 'has been killed'
                #print 'restarting', module, '...'
                inst_time = datetime.datetime.fromtimestamp(int(time.time()))
                printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "Killed"], 0))
                printarrayGlob.pop()
                #restart_module(module)
            else:
                #print 'killing failed!'
                inst_time = datetime.datetime.fromtimestamp(int(time.time()))
                printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "Killing failed!"], 0))
                printarrayGlob.pop()
    else:
        #print 'Module does not exist'
        inst_time = datetime.datetime.fromtimestamp(int(time.time()))
        printarrayGlob.insert(0, ([str(inst_time).split(' ')[1], module, pid, "Killing failed, module not found"], 0))
        printarrayGlob.pop()
    #time.sleep(5)
    cleanRedis()




def fetchQueueData():

    all_queue = set()
    printarray1 = []
    printarray2 = []
    printarray3 = []
    for queue, card in server.hgetall("queues").iteritems():
        all_queue.add(queue)
        key = "MODULE_" + queue + "_"
        keySet = "MODULE_TYPE_" + queue
        array_module_type = []
    
        for moduleNum in server.smembers(keySet):
            value = server.get(key + str(moduleNum))
            if value is not None:
                timestamp, path = value.split(", ")
                if timestamp is not None and path is not None:
                    startTime_readable = datetime.datetime.fromtimestamp(int(timestamp))
                    processed_time_readable = str((datetime.datetime.now() - startTime_readable)).split('.')[0]
                    if ((datetime.datetime.now() - startTime_readable).total_seconds()) > args.treshold:
                        QUEUE_STATUS[moduleNum] = 2
                    elif ((datetime.datetime.now() - startTime_readable).total_seconds()) > args.treshold/2:
                        QUEUE_STATUS[moduleNum] = 1
                    else:
                        QUEUE_STATUS[moduleNum] = 0
    
                    if int(card) > 0:
                        if int((datetime.datetime.now() - startTime_readable).total_seconds()) > args.treshold:
                            #log = open(log_filename, 'a')
                            #log.write(json.dumps([queue, card, str(startTime_readable), str(processed_time_readable), path]) + "\n")
                            try:
                                last_kill_try = time.time() - lastTimeKillCommand[moduleNum]
                            except KeyError:
                                last_kill_try = kill_retry_threshold+1
                            if args.autokill == 1 and last_kill_try > kill_retry_threshold :
                                kill_module(queue, int(moduleNum))
    
                        try:
                            cpu_percent = CPU_OBJECT_TABLE[int(moduleNum)].cpu_percent()
                            CPU_TABLE[moduleNum].insert(1, cpu_percent)
                            cpu_avg = sum(CPU_TABLE[moduleNum])/len(CPU_TABLE[moduleNum])
                            if len(CPU_TABLE[moduleNum]) > args.refresh*10:
                                CPU_TABLE[moduleNum].pop()
                            mem_percent = CPU_OBJECT_TABLE[int(moduleNum)].memory_percent()
                        except KeyError:
                            try:
                                CPU_OBJECT_TABLE[int(moduleNum)] = psutil.Process(int(moduleNum))
                                cpu_percent = CPU_OBJECT_TABLE[int(moduleNum)].cpu_percent()
                                CPU_TABLE[moduleNum] = []
                                cpu_avg = cpu_percent
                                mem_percent = CPU_OBJECT_TABLE[int(moduleNum)].memory_percent()
                            except psutil.NoSuchProcess:
                                cpu_percent = 0
                                cpu_avg = cpu_percent
                                mem_percent = 0

                        array_module_type.append( ([" <K>    [ ]", str(queue), str(moduleNum), str(card), str(startTime_readable), str(processed_time_readable), str(path), "{0:.2f}".format(cpu_percent)+"%", "{0:.2f}".format(mem_percent)+"%", "{0:.2f}".format(cpu_avg)+"%"], moduleNum) )
    
                    else:
                        printarray2.append( ([" <K>  ", str(queue), str(moduleNum), str(processed_time_readable), str(path)], moduleNum) )
                PID_NAME_DICO[int(moduleNum)] = str(queue)
                array_module_type.sort(lambda x,y: cmp(x[0][4], y[0][4]), reverse=True)
        for e in array_module_type:
            printarray1.append(e)
    
    for curr_queue in module_file_array:
        if curr_queue not in all_queue:
                printarray3.append( ([" <S>  ", curr_queue, "Not running by default"], curr_queue) )
        else:
            if len(list(server.smembers('MODULE_TYPE_'+curr_queue))) == 0:
                if curr_queue not in no_info_modules:
                    no_info_modules[curr_queue] = int(time.time())
                    printarray3.append( ([" <S>  ", curr_queue, "No data"], curr_queue) )
                else:
                    #If no info since long time, try to kill
                    if args.autokill == 1:
                        if int(time.time()) - no_info_modules[curr_queue] > args.treshold:
                            kill_module(curr_queue, None)
                            no_info_modules[curr_queue] = int(time.time())
                        printarray3.append( ([" <S>  ", curr_queue, "Stuck or idle, restarting in " + str(abs(args.treshold - (int(time.time()) - no_info_modules[curr_queue]))) + "s"], curr_queue) )
                    else:
                        printarray3.append( ([" <S>  ", curr_queue, "Stuck or idle, restarting disabled"], curr_queue) )
    
    
    printarray1.sort(key=lambda x: x[0], reverse=False)
    printarray2.sort(key=lambda x: x[0], reverse=False)

    printstring1 = format_string(printarray1, TABLES_PADDING["running"])
    printstring2 = format_string(printarray2, TABLES_PADDING["idle"])
    printstring3 = format_string(printarray3, TABLES_PADDING["notRunning"])

    return {"running": printstring1, "idle": printstring2, "notRunning": printstring3}

def format_string(tab, padding_row):
    printstring = []
    for row in tab:
        if row is None:
            continue
        the_array = row[0]
        the_pid = row[1]

        text=""
        for ite, elem in enumerate(the_array):
            if len(elem) > padding_row[ite]:
                text += "*" + elem[-padding_row[ite]+6:]
                padd_off = " "*5
            else:
                text += elem
                padd_off = " "*0
            text += (padding_row[ite] - len(elem))*" " + padd_off
        printstring.append( (text, the_pid) )
    return printstring



if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='Show info concerning running modules and log suspected stucked modules. May be use to automatically kill and restart stucked one.')
    parser.add_argument('-r', '--refresh', type=int, required=False, default=5, help='Refresh rate')
    parser.add_argument('-t', '--treshold', type=int, required=False, default=60*10*1, help='Refresh rate')
    parser.add_argument('-k', '--autokill', type=int, required=False, default=0, help='Enable auto kill option (1 for TRUE, anything else for FALSE)')
    parser.add_argument('-c', '--clear', type=int, required=False, default=0, help='Clear the current module information (Used to clear data from old launched modules)')

    args = parser.parse_args()

    configfile = os.path.join(os.environ['AIL_BIN'], 'packages/config.cfg')
    if not os.path.exists(configfile):
        raise Exception('Unable to find the configuration file. \
                        Did you set environment variables? \
                        Or activate the virtualenv.')

    cfg = ConfigParser.ConfigParser()
    cfg.read(configfile)

    # REDIS #
    server = redis.StrictRedis(
        host=cfg.get("Redis_Queues", "host"),
        port=cfg.getint("Redis_Queues", "port"),
        db=cfg.getint("Redis_Queues", "db"))

    if args.clear == 1:
        clearRedisModuleInfo()

    lastTime = datetime.datetime.now()

    module_file_array = set()
    no_info_modules = {}
    path_allmod = os.path.join(os.environ['AIL_HOME'], 'doc/all_modules.txt')
    with open(path_allmod, 'r') as module_file:
        for line in module_file:
            module_file_array.add(line[:-1])

    cleanRedis()

    
    TABLES_TITLES["running"] = format_string([([" Action", "Queue name", "PID", "#", "S Time", "R Time", "Processed element", "CPU %", "Mem %", "Avg CPU%"],0)], TABLES_PADDING["running"])[0][0]
    TABLES_TITLES["idle"] = format_string([([" Action", "Queue", "PID", "Idle Time", "Last paste hash"],0)], TABLES_PADDING["idle"])[0][0]
    TABLES_TITLES["notRunning"] = format_string([([" Action", "Queue", "State"],0)], TABLES_PADDING["notRunning"])[0][0]
    TABLES_TITLES["logs"] = format_string([(["Time", "Module", "PID", "Info"],0)], TABLES_PADDING["logs"])[0][0]


    while True:
       Screen.wrapper(demo)
       sys.exit(0)

