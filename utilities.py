import xml.etree.cElementTree as ET
from pkg_resources import parse_version
from urllib2 import urlopen, URLError
import win32evtlog
import win32evtlogutil
import win32security
import win32con
import winerror
import _winreg
import re
import string
import traceback
import datetime
from load_config import *
from subprocess import Popen, PIPE, call, check_output


msi_exit_dic = {"1619": "ERROR_INSTALL_PACKAGE_OPEN_FAILED",
                "1612": "ERROR_INSTALL_SOURCE_ABSENT"}



def client_running():
    n = 0
    prog=[line.split() for line in check_output("tasklist", creationflags=0x08000000).splitlines()]
    [prog.pop(e) for e in [0,1,2]] #clean up output and remove unwanted lines
    for entry in prog:
        if 'WPKG-GP-Client.exe' == entry[0]:
            n += 1
        else:
            continue
    if n > 1:
        return True
    else:
        return False

def shutdown(mode, time=60, msg=None):
    time = str(time)
    shutdown_base_str = "shutdown.exe "
    if mode == 1:
        shutdown_str = shutdown_base_str + "/f /r /t {}".format(time)
    elif mode == 2:
        shutdown_str = shutdown_base_str + "/f /s /t {}".format(time)
    elif mode == 3:
        shutdown_str = shutdown_base_str + "/a"
    else:
        print 'mode needs to be 1 = reboot, 2 = shutdown or 3 = cancel'
        return
    if mode < 3:
        if msg:
            if "%TIME%" in msg:
                msg = msg.replace("%TIME%", str(time))
            shutdown_str += ' /c "{}"'.format(str(msg))
    # Don't Display Console Window
    # Source: http://stackoverflow.com/questions/7006238/how-do-i-hide-the-console-when-i-use-os-system-or-subprocess-call
    CREATE_NO_WINDOW = 0x08000000
    call(shutdown_str, creationflags=CREATE_NO_WINDOW)

def SetRebootPendingTime(reset=False):
    if reset:
        now = "None"
    else:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _winreg.CreateKeyEx(_winreg.HKEY_LOCAL_MACHINE, R"SOFTWARE\Wpkg-GP-Client", 0,
                             _winreg.KEY_ALL_ACCESS | _winreg.KEY_WOW64_64KEY) as key:
        _winreg.SetValueEx(key, "RebootPending", 0, _winreg.REG_EXPAND_SZ, now)


def ReadRebootPendingTime():
    with _winreg.CreateKeyEx(_winreg.HKEY_LOCAL_MACHINE, R"SOFTWARE\Wpkg-GP-Client", 0,
                             _winreg.KEY_ALL_ACCESS | _winreg.KEY_WOW64_64KEY) as key:
        try:
            reboot_pending_value = _winreg.QueryValueEx(key, "RebootPending")[0]
        except WindowsError:
            return None
    try:
        reboot_pending_time = datetime.datetime.strptime(reboot_pending_value, '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None
    return reboot_pending_time

def vpn_connected(arch="x64"):
    if arch == "x64":
        vpn_path = "C:\Program Files (x86)\Cisco\Cisco AnyConnect Secure Mobility Client\\vpncli.exe"
    else:
        vpn_path = "C:\Program Files\Cisco\Cisco AnyConnect Secure Mobility Client\\vpncli.exe"
    p = Popen('"{}" -s state'.format(vpn_path), stdout=PIPE, stderr=PIPE, shell=True)
    out, err = p.communicate()
    if err:
        print 'error'
        print err # TODO: DEBUG
        return False
    else:
        if ">> notice: Connected to" in out:
            return True
        else:
            return False

def check_file_date(file):
    time = datetime.datetime.fromtimestamp(os.path.getmtime(file))
    return time

def getPercentage(str):
    pat = re.compile('\(([0-9]{1,3})\/([0-9]{1,3})\)')
    try:
        cur, max = re.search(pat, str).groups()
    except AttributeError, e:
        #print e
        progress = 1
    else:
        try:
            progress = (float(cur) / float(max)) * 100
        except ZeroDivisionError:
            progress = 1
    return int(progress)

def getBootUp():
    p = Popen('wmic os get lastbootuptime', stdout=PIPE, stderr=PIPE, shell=True)
    out, err = p.communicate()
    part_out = (out.split("\n", 1)[1]).split(".", 1)[0]
    bootup_time = datetime.datetime.strptime(part_out, '%Y%m%d%H%M%S')
    return bootup_time

def get_local_packages(xml_path):
    def resolve_variable(child, pkg_version):
        variable = re.compile('(%.+?%)').findall(pkg_version)
        variable = ''.join(variable)
        variable_name = re.sub('%', '', variable)
        value = 'None'
        try:
            for entry in child.iterfind(u'variable[@name="{}"]'.format(variable_name)):
                value = entry.attrib['value']
            return (variable, value)
        except TypeError:
            return (variable, value)
    tree = ET.parse(xml_path)
    root = tree.getroot()
    local_packages = {}
    for child in root.iter('package'):
        pkg_id = child.attrib['id']
        pkg_name = child.attrib['name']
        pkg_version = child.attrib['revision']
        if '%' in pkg_version:
                variable, value = resolve_variable(child, pkg_version)
                if '%' in value:
                    variable2, value2 = resolve_variable(child, value)
                    value = re.sub(variable2, value2, value)
                pkg_version = re.sub(variable, value, pkg_version)
        local_packages[pkg_id] = [pkg_name, pkg_version]
    return local_packages

def get_remote_packages(url):
    proxy = {"https" : "https://proxy.uni-hamburg.de:3128" }
    e = None
    try:
        xml = urlopen(url, timeout=5).read()
    except (IOError, URLError), e:
        print str(e)
        return {}, str(e)
    root = ET.fromstring(xml)
    remote_packages = {}
    for child in root.iter('package'):
        pkg_id = child.attrib['id']
        pkg_version = child.attrib['version']
        remote_packages[pkg_id] = pkg_version
    return remote_packages, e

def version_compare(local, remote):
    # Comparing Version Numbers:
    # http://stackoverflow.com/questions/11887762/compare-version-strings
    update_list = []
    for package in local:
        try:
            if parse_version(local[package][1]) < parse_version(remote[package]):
                update_list.append((local[package][0], remote[package]))
        except KeyError:
            continue
    return update_list

def check_eventlog(start_time):
    # Parse Windows EVENT LOG
    # Source: http://docs.activestate.com/activepython/3.3/pywin32/Windows_NT_Eventlog.html

    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ

    # This dict converts the event type into a human readable form
    evt_dict = {win32con.EVENTLOG_AUDIT_FAILURE: 'AUDIT_FAILURE',
                win32con.EVENTLOG_AUDIT_SUCCESS: 'AUDIT_SUCCESS',
                win32con.EVENTLOG_INFORMATION_TYPE: 'INFORMATION',
                win32con.EVENTLOG_WARNING_TYPE: 'WARNING',
                win32con.EVENTLOG_ERROR_TYPE: 'ERROR',
                0: 'INFORMATION'}
    computer = 'localhost'
    logtype = 'Application'
    time = "07/05/16 11:54:34"
        # open event log
    hand = win32evtlog.OpenEventLog(computer, logtype)
    #print logtype, ' events found since: ', start_time

    log = []
    error_log = []
    reboot = False

    try:
        events = 1
        while events:
            events = win32evtlog.ReadEventLog(hand, flags, 0)
            for ev_obj in events:
                the_time = ev_obj.TimeGenerated.Format()
                time_obj = datetime.datetime.strptime(the_time, '%m/%d/%y %H:%M:%S')
                if time_obj < start_time:
                    #if time is old than the start time dont grab the data
                    break
                # data is recent enough
                computer = str(ev_obj.ComputerName)
                src = str(ev_obj.SourceName)
                evt_type = str(evt_dict[ev_obj.EventType])
                msg = unicode(win32evtlogutil.SafeFormatMessage(ev_obj, logtype))

                if (src == 'WSH'):  # Only Append WPKG Logs (WSH - Windows Scripting Host)
                    log.append(string.join((the_time, computer, src, evt_type, '\n' + msg), ' : '))
                    if 'System reboot was initiated but overridden.' in msg:
                        reboot = True
                    if (evt_type == "ERROR") or (evt_type == "WARNING"):
                        # Only Append Errors and Warnings
                        if "msiexec" in msg:
                            try:
                                exit_code = re.compile("\(([0-9]|[0-9]{2}|[0-9]{4})\)").search(msg).groups()[0]
                                msg = msg + "MSI error ({}): {}".format(exit_code, msi_exit_dic[exit_code])
                            except (AttributeError, KeyError):
                                print 'Couldnt determine MSI Exit Code'
                        error_log.append(string.join((the_time, computer, src, evt_type, '\n' + msg), ' : '))

            if time_obj < start_time:
                break  # get out of while loop as well
        win32evtlog.CloseEventLog(hand)
    except:
        print traceback.print_exc(sys.exc_info())
    return log, error_log, reboot