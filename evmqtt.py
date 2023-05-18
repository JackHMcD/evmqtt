import os
import signal
import threading
import sys
import datetime
import json
from time import time, sleep
from platform import node as hostname
from pathlib import Path
import evdev
import paho.mqtt.client as mqtt


def log(s):
    sys.stderr.write("[%s] %s\n" % (datetime.datetime.now(), s))
    sys.stderr.flush()


class Watcher:
    def __init__(self):
        self.child = os.fork()
        if self.child == 0:
            return
        else:
            self.watch()

    def watch(self):
        try:
            os.wait()
        except KeyboardInterrupt:
            log('KeyboardInterrupt received')
            self.kill()
        sys.exit()

    def kill(self):
        try:
            os.kill(self.child, signal.SIGKILL)
        except OSError:
            pass


class MQTTClient(threading.Thread):
    def __init__(self, clientid, mqttcfg):
        super(MQTTClient, self).__init__()
        serverip = mqttcfg["serverip"]
        port = mqttcfg["port"]
        username = mqttcfg["username"]
        password = mqttcfg["password"]
        log("MQTT connecting to %s:%u" % (serverip, port))
        self.mqttclient = mqtt.Client(clientid, protocol=mqtt.MQTTv31)
        self.mqttclient.username_pw_set(username, password)
        self.mqttclient.on_connect = self.on_connect
        self.mqttclient.on_disconnect = self.on_disconnect
        self.mqttclient.on_message = self.on_message
        self.mqttclient.connect(serverip, port)
        self.mqttclient.loop_start()

    def on_connect(self, client, userdata, flags, rc):
        log("Connected with result code " + str(rc))
        client.subscribe("topic")

    def on_disconnect(self, client, userdata, rc):
        log("Disconnected with result code " + str(rc))

    def on_message(self, client, userdata, msg):
        msgpayload = str(msg.payload)
        print(msg.topic + " " + msgpayload)


class InputMonitor(threading.Thread):
    def __init__(self, mqttclient, device, topic):
        super(InputMonitor, self).__init__()
        self.mqttclient = mqttclient
        self.device_path = device
        self.topic = topic + '/state'  # state topic
        self.config = topic + '/config'  # config topic for HA autodiscovery
        self.config_data = {
            "name": MQTTCFG["name"],
            "state_topic": self.topic,
            "icon": "mdi:code-json"
        }
        self.is_device_available = True
        self.device = None
        self.last_event_time = 0
        self.reconnect_delay = 1

    def publish_config(self):
        msg_config = json.dumps(self.config_data)
        self.mqttclient.publish(self.config, msg_config)
        log("Sending configuration for autodiscovery to %s" % (self.config))

    def connect_device(self):
        try:
            self.device = evdev.InputDevice(self.device_path)
            self.device.grab()
            self.is_device_available = True
            self.publish_config()
            log("Monitoring %s and sending to topic %s" % (self.device_path, self.topic))
        except OSError as e:
            log("Exception occurred while connecting to device: %s" % e)
            self.is_device_available = False

    def reconnect_device(self):
        while not self.is_device_available:
            sleep(self.reconnect_delay)
            log("Waiting for the device to be available...")
            try:
                self.connect_device()
            except Exception as e:
                log("Exception occurred while reconnecting to device: %s" % e)

    def run(self):
        self.connect_device()
        while True:
            if self.is_device_available:
                try:
                    for event in self.device.read_loop():
                        if event.type == evdev.ecodes.EV_KEY:
                            self.handle_key_event(event)
                except OSError as e:
                    log("Exception occurred while reading device: %s" % e)
                    self.is_device_available = False
                    self.device.close()
                    self.reconnect_device()
            else:
                self.reconnect_device()

    def handle_key_event(self, event):
        current_time = time()
        if current_time - self.last_event_time >= 1:
            self.last_event_time = current_time
            k = evdev.categorize(event)
            key_state[k.keycode] = k.keystate
            if not is_modifier(k.keycode) and not is_ignore(k.keycode):
                if k.keystate == 1:
                    msg = {
                        "key": concat_multikeys(k.keycode) + get_modifiers(),
                        "devicePath": self.device_path
                    }
                    msg_json = json.dumps(msg)
                    self.mqttclient.publish(self.topic, msg_json)
                    log("Device '%s', published message %s" % (self.device_path, msg_json))


key_state = {}


def get_modifiers():
    global key_state
    ret = []
    for x in key_state.keys():
        if key_state[x] == 1:
            ret.append(x)
    ret.sort()
    if len(ret) == 0:
        return ""
    return "_" + "_".join(ret)


modifiers = [
    "KEY_LEFTSHIFT",
    "KEY_RIGHTSHIFT",
    "KEY_LEFTCTRL",
    "KEY_RIGHTCTRL"
]
ignore = ["KEY_NUMLOCK"]


def set_modifier(keycode, keystate):
    global key_state, modifiers
    if keycode in modifiers:
        key_state[keycode] = keystate


def is_modifier(keycode):
    global modifiers
    return keycode in modifiers


def is_ignore(keycode):
    global ignore
    return keycode in ignore


def concat_multikeys(keycode):
    ret = keycode
    if isinstance(ret, list):
        ret = "|".join(ret)
    return ret


if __name__ == "__main__":
    try:
        Watcher()

        config_filename = "config.local.json"
        config_file = Path(config_filename)
        if not config_file.is_file():
            config_filename = "config.json"

        log("Loading config from '%s'" % config_filename)
        MQTTCFG = json.load(open(config_filename))

        CLIENT = "evmqtt_{hostname}_{time}".format(hostname=hostname(), time=time())

        MQ = MQTTClient(CLIENT, MQTTCFG)
        MQ.start()

        topic = MQTTCFG["topic"]
        devices = MQTTCFG["devices"]

        available_devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        log("Found %s available devices:" % len(available_devices))
        for device in available_devices:
            log("Path:'%s', Name: '%s'" % (device.path, device.name))

        IM = [InputMonitor(MQ.mqttclient, device, topic) for device in devices]

        for monitor in IM:
            monitor.start()

    except (OSError, KeyError) as er:
        log("Exception: %s" % er)
