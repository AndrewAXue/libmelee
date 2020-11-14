"""The Console represents the engine running the game.

This can be Dolphin (Slippi's Ishiiruka) or an SLP file. The Console object
is your method to start and stop Dolphin, set configs, and get the latest GameState.
"""

from collections import defaultdict
from packaging import version

import time
import os
import configparser
import csv
import subprocess
import platform
import math
import base64
import numpy as np
from pathlib import Path

from melee import enums
from melee.gamestate import GameState, Projectile, Action, PlayerState
from melee.slippstream import SlippstreamClient, CommType, EventType
from melee.slpfilestreamer import SLPFileStreamer
from melee import stages


class SlippiVersionTooLow(Exception):
    """Raised when the Slippi version is not recent enough"""
    def __init__(self, message):
        self.message = message

# pylint: disable=too-many-instance-attributes
class Console:
    """The console object that represents your Dolphin / Wii / SLP file
    """
    def __init__(self,
                 path=None,
                 is_dolphin=True,
                 slippi_address="127.0.0.1",
                 slippi_port=51441,
                 online_delay=2,
                 blocking_input=False,
                 polling_mode=False,
                 allow_old_version=False,
                 logger=None):
        """Create a Console object

        Args:
            path (str): Path to the directory where your dolphin executable is located.
                If None, will assume the dolphin is remote and won't try to configure it.
            slippi_address (str): IP address of the Dolphin / Wii to connect to.
            slippi_port (int): UDP port that slippi will listen on
            online_delay (int): How many frames of delay to apply in online matches
            blocking_input (bool): Should dolphin block waiting for bot input
                This is only really useful if you're doing ML training.
            polling_mode (bool): Polls input to console rather than blocking for it
                When set, step() will always return immediately, but may be None if no
                gamestate is available yet.
            allow_old_version (bool): Allow SLP versions older than 3.0.0 (rollback era)
                Only enable if you know what you're doing. You probably don't want this.
                Gamestates will be missing key information, come in really late, or possibly not work at all
            logger (logger.Logger): Logger instance to use. None for no logger.
        """
        self.logger = logger
        self.is_dolphin = is_dolphin
        self.path = path
        self.processingtime = 0
        self._frametimestamp = time.time()
        self.slippi_address = slippi_address
        """(str): IP address of the Dolphin / Wii to connect to."""
        self.slippi_port = slippi_port
        """(int): UDP port of slippi server. Default 51441"""
        self.eventsize = [0] * 0x100
        self.connected = False
        self.nick = ""
        """(str): The nickname the console has given itself."""
        self.version = ""
        """(str): The Slippi version of the console"""
        self.cursor = 0
        self.controllers = []
        self._current_stage = enums.Stage.NO_STAGE
        self._frame = 0
        self._polling_mode = polling_mode
        self.slp_version = "unknown"
        """(str): The SLP version this stream/file currently is."""
        self._allow_old_version = allow_old_version
        self._use_manual_bookends = False
        self._costumes = {0:0, 1:0, 2:0, 3:0}
        self._cpu_level = {0:0, 1:0, 2:0, 3:0}

        # Keep a running copy of the last gamestate produced
        self._prev_gamestate = GameState()
        # Half-completed gamestate not yet ready to add to the list
        self._temp_gamestate = None
        self._process = None
        if self.is_dolphin:
            self._slippstream = SlippstreamClient(self.slippi_address, self.slippi_port)
            if self.path:
                # Setup some dolphin config options
                dolphin_config_path = self._get_dolphin_config_path() + "Dolphin.ini"
                config = configparser.SafeConfigParser()
                config.read(dolphin_config_path)
                config.set("Core", 'slippienablespectator', "True")
                config.set("Core", 'slippispectatorlocalport', str(self.slippi_port))
                # Set online delay
                config.set("Core", 'slippionlinedelay', str(online_delay))
                # Turn on background input so we don't need to have window focus on dolphin
                config.set("Input", 'backgroundinput', "True")
                config.set("Core", 'BlockingPipes', str(blocking_input))
                with open(dolphin_config_path, 'w') as dolphinfile:
                    config.write(dolphinfile)
        else:
            self._slippstream = SLPFileStreamer(self.path)

        # Prepare some structures for fixing melee data
        path = os.path.dirname(os.path.realpath(__file__))
        with open(path + "/actiondata.csv") as csvfile:
            #A list of dicts containing the frame data
            actiondata = list(csv.DictReader(csvfile))
            #Dict of sets
            self.zero_indices = defaultdict(set)
            for line in actiondata:
                if line["zeroindex"] == "True":
                    self.zero_indices[int(line["character"])].add(int(line["action"]))

        # Read the character data csv
        self.characterdata = dict()
        with open(path + "/characterdata.csv") as csvfile:
            reader = csv.DictReader(csvfile)
            for line in reader:
                del line["Character"]
                #Convert all fields to numbers
                for key, value in line.items():
                    line[key] = float(value)
                self.characterdata[enums.Character(line["CharacterIndex"])] = line

    def connect(self):
        """ Connects to the Slippi server (dolphin or wii).

        Returns:
            True is successful, False otherwise
        """
        # It can take a short amount of time after starting the emulator
        #   for the actual server to start. So try a few times before giving up.
        for _ in range(4):
            if self._slippstream.connect():
                return True
        return False

    def run(self, iso_path=None, dolphin_config_path=None):
        """Run the Dolphin emulator.

        This starts the Dolphin process, so don't run this if you're connecting to an
        already running Dolphin instance.

        Args:
            iso_path (str, optional): Path to Melee ISO for dolphin to read
            dolphin_config_path (str, optional): Alternative config path for dolphin
                if not using the default
        """
        if self.is_dolphin and self.path:
            exe_name = "dolphin-emu"
            if platform.system() == "Windows":
                exe_name = "Dolphin.exe"

            exe_path = ""
            if self.path:
                exe_path = self.path
            command = [exe_path + "/" + exe_name]
            if platform.system() == "Linux" and os.path.isfile(self.path):
                command = [self.path]
            if iso_path is not None:
                command.append("-e")
                command.append(iso_path)
            if dolphin_config_path is not None:
                command.append("-u")
                command.append(dolphin_config_path)
            self._process = subprocess.Popen(command)

    def stop(self):
        """ Stop the console.

        For Dolphin instances, this will kill the dolphin process.
        For Wiis and SLP files, it just shuts down our connection
         """
        if self.path:
            self.connected = False
            self._slippstream.shutdown()
            # If dolphin, kill the process
            if self._process is not None:
                self._process.terminate()

    def setup_dolphin_controller(self, port, controllertype=enums.ControllerType.STANDARD):
        """Setup the necessary files for dolphin to recognize the player at the given
        controller port and type"""

        pipes_path = self.get_dolphin_pipes_path(port)
        if platform.system() != "Windows" and controllertype == enums.ControllerType.STANDARD:
            if not os.path.exists(pipes_path):
                os.mkfifo(pipes_path)

        #Read in dolphin's controller config file
        controller_config_path = self._get_dolphin_config_path() + "GCPadNew.ini"
        config = configparser.SafeConfigParser()
        config.read(controller_config_path)

        #Add a bot standard controller config to the given port
        section = "GCPad" + str(port)
        if not config.has_section(section):
            config.add_section(section)

        if controllertype == enums.ControllerType.STANDARD:
            config.set(section, 'Device', 'Pipe/0/slippibot' + str(port))
            config.set(section, 'Buttons/A', 'Button A')
            config.set(section, 'Buttons/B', 'Button B')
            config.set(section, 'Buttons/X', 'Button X')
            config.set(section, 'Buttons/Y', 'Button Y')
            config.set(section, 'Buttons/Z', 'Button Z')
            config.set(section, 'Buttons/L', 'Button L')
            config.set(section, 'Buttons/R', 'Button R')
            config.set(section, 'Main Stick/Up', 'Axis MAIN Y +')
            config.set(section, 'Main Stick/Down', 'Axis MAIN Y -')
            config.set(section, 'Main Stick/Left', 'Axis MAIN X -')
            config.set(section, 'Main Stick/Right', 'Axis MAIN X +')
            config.set(section, 'Triggers/L', 'Button L')
            config.set(section, 'Triggers/R', 'Button R')
            config.set(section, 'Main Stick/Modifier', 'Shift_L')
            config.set(section, 'Main Stick/Modifier/Range', '50.000000000000000')
            config.set(section, 'D-Pad/Up', 'Button D_UP')
            config.set(section, 'D-Pad/Down', 'Button D_DOWN')
            config.set(section, 'D-Pad/Left', 'Button D_LEFT')
            config.set(section, 'D-Pad/Right', 'Button D_RIGHT')
            config.set(section, 'Buttons/Start', 'Button START')
            config.set(section, 'Buttons/A', 'Button A')
            config.set(section, 'C-Stick/Up', 'Axis C Y +')
            config.set(section, 'C-Stick/Down', 'Axis C Y -')
            config.set(section, 'C-Stick/Left', 'Axis C X -')
            config.set(section, 'C-Stick/Right', 'Axis C X +')
            config.set(section, 'Triggers/L-Analog', 'Axis L -+')
            config.set(section, 'Triggers/R-Analog', 'Axis R -+')
        #This section is unused if it's not a standard input (I think...)
        else:
            config.set(section, 'Device', 'XInput2/0/Virtual core pointer')

        with open(controller_config_path, 'w') as configfile:
            config.write(configfile)

        dolphin_config_path = self._get_dolphin_config_path() + "Dolphin.ini"
        config = configparser.SafeConfigParser()
        config.read(dolphin_config_path)
        # Indexed at 0. "6" means standard controller, "12" means GCN Adapter
        #  The enum is scoped to the proper value, here
        config.set("Core", 'SIDevice'+str(port-1), controllertype.value)
        with open(dolphin_config_path, 'w') as dolphinfile:
            config.write(dolphinfile)

    def step(self):
        """ 'step' to the next state of the game and flushes all controllers

        Returns:
            GameState object that represents new current state of the game"""
        self.processingtime = time.time() - self._frametimestamp

        # Flush the controllers
        for controler in self.controllers:
            controler.flush()

        if self._temp_gamestate is None:
            self._temp_gamestate = GameState()

        frame_ended = False
        while not frame_ended:
            message = self._slippstream.dispatch(self._polling_mode)
            if message:
                if message["type"] == "connect_reply":
                    self.connected = True
                    self.nick = message["nick"]
                    self.version = message["version"]
                    self.cursor = message["cursor"]

                elif message["type"] == "game_event":
                    if len(message["payload"]) > 0:
                        if self.is_dolphin:
                            frame_ended = self.__handle_slippstream_events(base64.b64decode(message["payload"]), self._temp_gamestate)
                        else:
                            frame_ended = self.__handle_slippstream_events(message["payload"], self._temp_gamestate)

                elif message["type"] == "menu_event":
                    if len(message["payload"]) > 0:
                        self.__handle_slippstream_menu_event(base64.b64decode(message["payload"]), self._temp_gamestate)
                        frame_ended = True

                elif self._use_manual_bookends and message["type"] == "frame_end" and self._frame != -10000:
                    frame_ended = True
            else:
                return None

        gamestate = self._temp_gamestate
        self._temp_gamestate = None
        self.__fixframeindexing(gamestate)
        self.__fixiasa(gamestate)
        # Start the processing timer now that we're done reading messages
        self._frametimestamp = time.time()
        return gamestate

    def __handle_slippstream_events(self, event_bytes, gamestate):
        """ Handle a series of events, provided sequentially in a byte array """
        gamestate.menu_state = enums.Menu.IN_GAME
        while len(event_bytes) > 0:
            event_size = self.eventsize[event_bytes[0]]
            if len(event_bytes) < event_size:
                print("WARNING: Something went wrong unpacking events. Data is probably missing")
                print("\tDidn't have enough data for event")
                return False
            if EventType(event_bytes[0]) == EventType.PAYLOADS:
                cursor = 0x2
                payload_size = event_bytes[1]
                num_commands = (payload_size - 1) // 3
                for i in range(0, num_commands):
                    command = np.ndarray((1,), ">B", event_bytes, cursor)[0]
                    command_len = np.ndarray((1,), ">H", event_bytes, cursor + 0x1)[0]
                    self.eventsize[command] = command_len+1
                    cursor += 3
                event_bytes = event_bytes[payload_size + 1:]

            elif EventType(event_bytes[0]) == EventType.FRAME_START:
                event_bytes = event_bytes[event_size:]

            elif EventType(event_bytes[0]) == EventType.GAME_START:
                self.__game_start(gamestate, event_bytes)
                event_bytes = event_bytes[event_size:]

            elif EventType(event_bytes[0]) == EventType.GAME_END:
                event_bytes = event_bytes[event_size:]
                return self._use_manual_bookends

            elif EventType(event_bytes[0]) == EventType.PRE_FRAME:
                self.__pre_frame(gamestate, event_bytes)
                event_bytes = event_bytes[event_size:]

            elif EventType(event_bytes[0]) == EventType.POST_FRAME:
                self.__post_frame(gamestate, event_bytes)
                event_bytes = event_bytes[event_size:]

            elif EventType(event_bytes[0]) == EventType.GECKO_CODES:
                event_bytes = event_bytes[event_size:]

            elif EventType(event_bytes[0]) == EventType.FRAME_BOOKEND:
                self.__frame_bookend(gamestate, event_bytes)
                event_bytes = event_bytes[event_size:]
                # If this is an old frame, then don't return it.
                if gamestate.frame <= self._frame:
                    return False
                self._frame = gamestate.frame
                return True

            elif EventType(event_bytes[0]) == EventType.ITEM_UPDATE:
                self.__item_update(gamestate, event_bytes)
                event_bytes = event_bytes[event_size:]

            else:
                print("WARNING: Something went wrong unpacking events. " + \
                    "Data is probably missing")
                print("\tGot invalid event type: ", event_bytes[0])
                return False
        return False

    def __game_start(self, gamestate, event_bytes):
        self._frame = -10000
        major = np.ndarray((1,), ">B", event_bytes, 0x1)[0]
        minor = np.ndarray((1,), ">B", event_bytes, 0x2)[0]
        version_num = np.ndarray((1,), ">B", event_bytes, 0x3)[0]
        self.slp_version = str(major) + "." + str(minor) + "." + str(version_num)
        self._use_manual_bookends = self._allow_old_version and (version.parse(self.slp_version) < version.parse("3.0.0"))
        if major < 3 and not self._allow_old_version:
            raise SlippiVersionTooLow(self.slp_version)
        try:
            self._current_stage = enums.to_internal_stage(np.ndarray((1,), ">H", event_bytes, 0x13)[0])
        except ValueError:
            self._current_stage = enums.Stage.NO_STAGE

        for i in range(4):
            self._costumes[i] = np.ndarray((1,), ">B", event_bytes, 0x68 + (0x24 * i))[0]

        for i in range(4):
            self._cpu_level[i] = np.ndarray((1,), ">B", event_bytes, 0x74 + (0x24 * i))[0]

        for i in range(4):
            if np.ndarray((1,), ">B", event_bytes, 0x66 + (0x24 * i))[0] != 1:
                self._cpu_level[i] = 0

    def __pre_frame(self, gamestate, event_bytes):
        # Grab the physical controller state and put that into the controller state
        controller_port = np.ndarray((1,), ">B", event_bytes, 0x5)[0] + 1

        if controller_port not in gamestate.player:
            gamestate.player[controller_port] = PlayerState()
        playerstate = gamestate.player[controller_port]

        playerstate.costume = self._costumes[controller_port-1]
        playerstate.cpu_level = self._cpu_level[controller_port-1]

        main_x = (np.ndarray((1,), ">f", event_bytes, 0x19)[0] / 2) + 0.5
        main_y = (np.ndarray((1,), ">f", event_bytes, 0x1D)[0] / 2) + 0.5
        playerstate.controller_state.main_stick = (main_x, main_y)

        c_x = (np.ndarray((1,), ">f", event_bytes, 0x21)[0] / 2) + 0.5
        c_y = (np.ndarray((1,), ">f", event_bytes, 0x25)[0] / 2) + 0.5
        playerstate.controller_state.c_stick = (c_x, c_y)

        buttonbits = np.ndarray((1,), ">H", event_bytes, 0x31)[0]
        playerstate.controller_state.button[enums.Button.BUTTON_A] = bool(int(buttonbits) & 0x0100)
        playerstate.controller_state.button[enums.Button.BUTTON_B] = bool(int(buttonbits) & 0x0200)
        playerstate.controller_state.button[enums.Button.BUTTON_X] = bool(int(buttonbits) & 0x0400)
        playerstate.controller_state.button[enums.Button.BUTTON_Y] = bool(int(buttonbits) & 0x0800)
        playerstate.controller_state.button[enums.Button.BUTTON_START] = bool(int(buttonbits) & 0x1000)
        playerstate.controller_state.button[enums.Button.BUTTON_Z] = bool(int(buttonbits) & 0x0010)
        playerstate.controller_state.button[enums.Button.BUTTON_R] = bool(int(buttonbits) & 0x0020)
        playerstate.controller_state.button[enums.Button.BUTTON_L] = bool(int(buttonbits) & 0x0040)
        playerstate.controller_state.button[enums.Button.BUTTON_D_LEFT] = bool(int(buttonbits) & 0x0001)
        playerstate.controller_state.button[enums.Button.BUTTON_D_RIGHT] = bool(int(buttonbits) & 0x0002)
        playerstate.controller_state.button[enums.Button.BUTTON_D_DOWN] = bool(int(buttonbits) & 0x0004)
        playerstate.controller_state.button[enums.Button.BUTTON_D_UP] = bool(int(buttonbits) & 0x0008)
        if self._use_manual_bookends:
            self._frame = gamestate.frame

    def __post_frame(self, gamestate, event_bytes):
        gamestate.stage = self._current_stage
        gamestate.frame = np.ndarray((1,), ">i", event_bytes, 0x1)[0]
        controller_port = np.ndarray((1,), ">B", event_bytes, 0x5)[0] + 1

        if controller_port not in gamestate.player:
            gamestate.player[controller_port] = PlayerState()

        playerstate = gamestate.player[controller_port]
        playerstate.x = np.ndarray((1,), ">f", event_bytes, 0xa)[0]
        playerstate.y = np.ndarray((1,), ">f", event_bytes, 0xe)[0]

        playerstate.character = enums.Character(np.ndarray((1,), ">B", event_bytes, 0x7)[0])
        try:
            playerstate.action = enums.Action(np.ndarray((1,), ">H", event_bytes, 0x8)[0])
        except ValueError:
            playerstate.action = enums.Action.UNKNOWN_ANIMATION

        # Melee stores this in a float for no good reason. So we have to convert
        playerstate.facing = np.ndarray((1,), ">f", event_bytes, 0x12)[0] > 0

        playerstate.percent = int(np.ndarray((1,), ">f", event_bytes, 0x16)[0])
        playerstate.shield_strength = np.ndarray((1,), ">f", event_bytes, 0x1A)[0]
        playerstate.stock = np.ndarray((1,), ">B", event_bytes, 0x21)[0]
        playerstate.action_frame = int(np.ndarray((1,), ">f", event_bytes, 0x22)[0])

        # Extract the bit at mask 0x20
        try:
            bitflags2 = np.ndarray((1,), ">B", event_bytes, 0x27)[0]
            playerstate.hitlag = bool(bitflags2 & 0x20)
        except TypeError:
            playerstate.hitlag = False

        try:
            playerstate.hitstun_frames_left = int(np.ndarray((1,), ">f", event_bytes, 0x2B)[0])
        except TypeError:
            playerstate.hitstun_frames_left = 0
        except ValueError:
            playerstate.hitstun_frames_left = 0
        try:
            playerstate.on_ground = not bool(np.ndarray((1,), ">B", event_bytes, 0x2F)[0])
        except TypeError:
            playerstate.on_ground = True
        try:
            playerstate.jumps_left = np.ndarray((1,), ">B", event_bytes, 0x32)[0]
        except TypeError:
            playerstate.jumps_left = 1

        try:
            playerstate.invulnerable = int(np.ndarray((1,), ">B", event_bytes, 0x34)[0]) != 0
        except TypeError:
            playerstate.invulnerable = False

        try:
            playerstate.speed_air_x_self = np.ndarray((1,), ">f", event_bytes, 0x35)[0]
        except TypeError:
            playerstate.speed_air_x_self = 0

        try:
            playerstate.speed_y_self = np.ndarray((1,), ">f", event_bytes, 0x39)[0]
        except TypeError:
            playerstate.speed_y_self = 0

        try:
            playerstate.speed_x_attack = np.ndarray((1,), ">f", event_bytes, 0x3D)[0]
        except TypeError:
            playerstate.speed_x_attack = 0

        try:
            playerstate.speed_y_attack = np.ndarray((1,), ">f", event_bytes, 0x41)[0]
        except TypeError:
            playerstate.speed_y_attack = 0

        try:
            playerstate.speed_ground_x_self = np.ndarray((1,), ">f", event_bytes, 0x45)[0]
        except TypeError:
            playerstate.speed_ground_x_self = 0

        # Keep track of a player's invulnerability due to respawn or ledge grab
        if controller_port in self._prev_gamestate.player:
            playerstate.invulnerability_left = max(0, self._prev_gamestate.player[controller_port].invulnerability_left - 1)
        if playerstate.action == Action.ON_HALO_WAIT:
            playerstate.invulnerability_left = 120
        # Don't give invulnerability to the first descent
        if playerstate.action == Action.ON_HALO_DESCENT and gamestate.frame > 150:
            playerstate.invulnerability_left = 120
        if playerstate.action == Action.EDGE_CATCHING and playerstate.action_frame == 1:
            playerstate.invulnerability_left = 36

        # The pre-warning occurs when we first start a dash dance.
        if controller_port in self._prev_gamestate.player:
            if playerstate.action == Action.DASHING and \
                    self._prev_gamestate.player[controller_port].action not in [Action.DASHING, Action.TURNING]:
                playerstate.moonwalkwarning = True

        # Take off the warning if the player does an action other than dashing
        if playerstate.action != Action.DASHING:
            playerstate.moonwalkwarning = False

        # "off_stage" helper
        try:
            if (abs(playerstate.x) > stages.EDGE_GROUND_POSITION[gamestate.stage] or \
                    playerstate.y < -6) and not playerstate.on_ground:
                playerstate.off_stage = True
            else:
                playerstate.off_stage = False
        except KeyError:
            playerstate.off_stage = False

        # ECB top edge, x
        ecb_top_x = 0
        ecb_top_y = 0
        try:
            ecb_top_x = np.ndarray((1,), ">f", event_bytes, 0x49)[0]
        except TypeError:
            ecb_top_x = 0
        # ECB Top edge, y
        try:
            ecb_top_y = np.ndarray((1,), ">f", event_bytes, 0x4D)[0]
        except TypeError:
            ecb_top_y = 0
        playerstate.ecb_top = (ecb_top_x, ecb_top_y)

        # ECB bottom edge, x coord
        ecb_bot_x = 0
        ecb_bot_y = 0
        try:
            ecb_bot_x = np.ndarray((1,), ">f", event_bytes, 0x51)[0]
        except TypeError:
            ecb_bot_x = 0
        # ECB Bottom edge, y coord
        try:
            ecb_bot_y = np.ndarray((1,), ">f", event_bytes, 0x55)[0]
        except TypeError:
            ecb_bot_y = 0
        playerstate.ecb_bottom = (ecb_bot_x, ecb_bot_y)

        # ECB left edge, x coord
        ecb_left_x = 0
        ecb_left_y = 0
        try:
            ecb_left_x = np.ndarray((1,), ">f", event_bytes, 0x59)[0]
        except TypeError:
            ecb_left_x = 0
        # ECB left edge, y coord
        try:
            ecb_left_y = np.ndarray((1,), ">f", event_bytes, 0x5D)[0]
        except TypeError:
            ecb_left_y = 0
        playerstate.ecb_left = (ecb_left_x, ecb_left_y)

        # ECB right edge, x coord
        ecb_right_x = 0
        ecb_right_y = 0
        try:
            ecb_right_x = np.ndarray((1,), ">f", event_bytes, 0x61)[0]
        except TypeError:
            ecb_right_x = 0
        # ECB right edge, y coord
        try:
            ecb_right_y = np.ndarray((1,), ">f", event_bytes, 0x65)[0]
        except TypeError:
            ecb_right_y = 0
        playerstate.ecb_right = (ecb_right_x, ecb_right_y)
        if self._use_manual_bookends:
            self._frame = gamestate.frame

    def __frame_bookend(self, gamestate, event_bytes):
        self._prev_gamestate = gamestate
        # Calculate helper distance variable
        #   This is a bit kludgey.... :/
        i = 0
        player_one_x, player_one_y, player_two_x, player_two_y = 0, 0, 0, 0
        for _, player_state in gamestate.player.items():
            if i == 0:
                player_one_x, player_one_y = player_state.x, player_state.y
            if i == 1:
                player_two_x, player_two_y = player_state.x, player_state.y
            i += 1
        xdist = player_one_x - player_two_x
        ydist = player_one_y - player_two_y
        gamestate.distance = math.sqrt((xdist**2) + (ydist**2))

    def __item_update(self, gamestate, event_bytes):
        projectile = Projectile()
        projectile.x = np.ndarray((1,), ">f", event_bytes, 0x14)[0]
        projectile.y = np.ndarray((1,), ">f", event_bytes, 0x18)[0]
        projectile.x_speed = np.ndarray((1,), ">f", event_bytes, 0xc)[0]
        projectile.y_speed = np.ndarray((1,), ">f", event_bytes, 0x10)[0]
        try:
            projectile.owner = np.ndarray((1,), ">B", event_bytes, 0x2A)[0] + 1
            if projectile.owner > 4:
                projectile.owner = -1
        except TypeError:
            projectile.owner = -1
        try:
            projectile.subtype = enums.ProjectileSubtype(np.ndarray((1,), ">H", event_bytes, 0x5)[0])
        except ValueError:
            projectile.subtype = enums.ProjectileSubtype.UNKNOWN_PROJECTILE
        # Add the projectile to the gamestate list
        gamestate.projectiles.append(projectile)

    def __handle_slippstream_menu_event(self, event_bytes, gamestate):
        """ Internal handler for slippstream menu events

        Modifies specified gamestate based on the event bytes
         """
        scene = np.ndarray((1,), ">H", event_bytes, 0x1)[0]
        if scene == 0x02:
            gamestate.menu_state = enums.Menu.CHARACTER_SELECT
            # All the controller ports are active on this screen
            gamestate.player[1] = PlayerState()
            gamestate.player[2] = PlayerState()
            gamestate.player[3] = PlayerState()
            gamestate.player[4] = PlayerState()
        elif scene in [0x0102, 0x0108]:
            gamestate.menu_state = enums.Menu.STAGE_SELECT
        elif scene == 0x0202:
            gamestate.menu_state = enums.Menu.IN_GAME
        elif scene == 0x0001:
            gamestate.menu_state = enums.Menu.MAIN_MENU
        elif scene == 0x0008:
            gamestate.menu_state = enums.Menu.SLIPPI_ONLINE_CSS
            gamestate.player[1] = PlayerState()
            gamestate.player[2] = PlayerState()
            gamestate.player[3] = PlayerState()
            gamestate.player[4] = PlayerState()
        elif scene == 0x0000:
            gamestate.menu_state = enums.Menu.PRESS_START
        else:
            gamestate.menu_state = enums.Menu.UNKNOWN_MENU

        # controller port statuses at CSS
        if gamestate.menu_state in [enums.Menu.CHARACTER_SELECT, enums.Menu.SLIPPI_ONLINE_CSS]:
            gamestate.player[1].controller_status = enums.ControllerStatus(np.ndarray((1,), ">B", event_bytes, 0x25)[0])
            gamestate.player[2].controller_status = enums.ControllerStatus(np.ndarray((1,), ">B", event_bytes, 0x26)[0])
            gamestate.player[3].controller_status = enums.ControllerStatus(np.ndarray((1,), ">B", event_bytes, 0x27)[0])
            gamestate.player[4].controller_status = enums.ControllerStatus(np.ndarray((1,), ">B", event_bytes, 0x28)[0])

            # CSS Cursors
            gamestate.player[1].cursor_x = np.ndarray((1,), ">f", event_bytes, 0x3)[0]
            gamestate.player[1].cursor_y = np.ndarray((1,), ">f", event_bytes, 0x7)[0]
            gamestate.player[2].cursor_x = np.ndarray((1,), ">f", event_bytes, 0xB)[0]
            gamestate.player[2].cursor_y = np.ndarray((1,), ">f", event_bytes, 0xF)[0]
            gamestate.player[3].cursor_x = np.ndarray((1,), ">f", event_bytes, 0x13)[0]
            gamestate.player[3].cursor_y = np.ndarray((1,), ">f", event_bytes, 0x17)[0]
            gamestate.player[4].cursor_x = np.ndarray((1,), ">f", event_bytes, 0x1B)[0]
            gamestate.player[4].cursor_y = np.ndarray((1,), ">f", event_bytes, 0x1F)[0]

            # Ready to fight banner
            gamestate.ready_to_start = np.ndarray((1,), ">B", event_bytes, 0x23)[0]

            # Character selected
            try:
                gamestate.player[1].character_selected = enums.to_internal(np.ndarray((1,), ">B", event_bytes, 0x29)[0])
            except TypeError:
                gamestate.player[1].character_selected = enums.Character.UNKNOWN_CHARACTER
            try:
                gamestate.player[2].character_selected = enums.to_internal(np.ndarray((1,), ">B", event_bytes, 0x2A)[0])
            except TypeError:
                gamestate.player[2].character_selected = enums.Character.UNKNOWN_CHARACTER
            try:
                gamestate.player[3].character_selected = enums.to_internal(np.ndarray((1,), ">B", event_bytes, 0x2B)[0])
            except TypeError:
                gamestate.player[3].character_selected = enums.Character.UNKNOWN_CHARACTER
            try:
                gamestate.player[4].character_selected = enums.to_internal(np.ndarray((1,), ">B", event_bytes, 0x2C)[0])
            except TypeError:
                gamestate.player[4].character_selected = enums.Character.UNKNOWN_CHARACTER

            # Coin down
            try:
                gamestate.player[1].coin_down = np.ndarray((1,), ">B", event_bytes, 0x2D)[0] == 2
            except TypeError:
                gamestate.player[1].coin_down = False
            try:
                gamestate.player[2].coin_down = np.ndarray((1,), ">B", event_bytes, 0x2E)[0] == 2
            except TypeError:
                gamestate.player[2].coin_down = False
            try:
                gamestate.player[3].coin_down = np.ndarray((1,), ">B", event_bytes, 0x2F)[0] == 2
            except TypeError:
                gamestate.player[3].coin_down = False
            try:
                gamestate.player[4].coin_down = np.ndarray((1,), ">B", event_bytes, 0x30)[0] == 2
            except TypeError:
                gamestate.player[4].coin_down = False

        if gamestate.menu_state == enums.Menu.STAGE_SELECT:
            # Stage
            try:
                gamestate.stage = enums.Stage(np.ndarray((1,), ">B", event_bytes, 0x24)[0])
            except ValueError:
                gamestate.stage = enums.Stage.NO_STAGE

            # Stage Select Cursor X, Y
            gamestate.stage_select_cursor_x = np.ndarray((1,), ">f", event_bytes, 0x31)[0]
            gamestate.stage_select_cursor_y = np.ndarray((1,), ">f", event_bytes, 0x35)[0]

        # Frame count
        gamestate.frame = np.ndarray((1,), ">i", event_bytes, 0x39)[0]

        # Sub-menu
        try:
            gamestate.submenu = enums.SubMenu(np.ndarray((1,), ">B", event_bytes, 0x3D)[0])
        except TypeError:
            gamestate.submenu = enums.SubMenu.UNKNOWN_SUBMENU
        except ValueError:
            gamestate.submenu = enums.SubMenu.UNKNOWN_SUBMENU

        # Selected menu
        try:
            gamestate.menu_selection = np.ndarray((1,), ">B", event_bytes, 0x3E)[0]
        except TypeError:
            gamestate.menu_selection = 0

        # Online costume chosen
        try:
            if gamestate.menu_state == enums.Menu.SLIPPI_ONLINE_CSS:
                for i in range(4):
                    gamestate.player[i+1].costume = np.ndarray((1,), ">B", event_bytes, 0x3F)[0]
        except TypeError:
            pass

        # This value is 0x05 in the nametag entry
        try:
            if gamestate.menu_state == enums.Menu.SLIPPI_ONLINE_CSS:
                nametag = np.ndarray((1,), ">B", event_bytes, 0x40)[0]
                if nametag == 0x05:
                    gamestate.submenu = enums.SubMenu.NAME_ENTRY_SUBMENU
                elif nametag == 0x00:
                    gamestate.submenu = enums.SubMenu.ONLINE_CSS
        except TypeError:
            pass

        # Is Holding CPU Slider
        try:
            for i in range(4):
                gamestate.player[i+1].is_holding_cpu_slider = gamestate.player[i+1].cursor_y < 0 and np.ndarray((1,), ">B", event_bytes, 45 + i)[0]
        except TypeError:
            pass
        except KeyError:
            pass

        # CPU Level
        try:
            for i in range(4):
                if gamestate.player[i+1].is_holding_cpu_slider:
                    start_x = -30.9 + (15.4 * i)
                    gamestate.player[i+1].cpu_level = 1 + int((gamestate.player[i + 1].cursor_x - start_x)/1.2)
                else:
                    gamestate.player[i + 1].cpu_level = 1
        except TypeError:
            pass
        except KeyError:
            pass

        # Set CPU level to 0 if we're not a CPU
        for port in gamestate.player:
            if gamestate.player[port].controller_status != enums.ControllerStatus.CONTROLLER_CPU:
                gamestate.player[port].cpu_level = 0

    def _get_dolphin_home_path(self):
        """Return the path to dolphin's home directory"""
        if self.path:
            return self.path + "/User/"
        return ""

    def _get_dolphin_config_path(self):
        """ Return the path to dolphin's config directory
        (which is not necessarily the same as the home path)"""
        if self.path:
            if platform.system() == "Linux":
                # First check if the config path is here in the same directory
                if os.path.isdir(self.path + "/User/Config/"):
                    return self.path + "/User/Config/"
                # Otherwise, this must be an appimage install. Use the .config
                return str(Path.home()) + "/.config/SlippiOnline/Config/"
            else:
                return self.path + "/User/Config/"
        return ""

    def get_dolphin_pipes_path(self, port):
        """Get the path of the named pipe input file for the given controller port
        """
        if platform.system() == "Windows":
            return '\\\\.\\pipe\\slippibot' + str(port)
        if platform.system() == "Linux":
            # First check if the config path is here in the same directory
            if os.path.isdir(self.path + "/User/"):
                if not os.path.isdir(self.path + "/User/Pipes/"):
                    os.mkdir(self.path + "/User/Pipes/")
                return self.path + "/User/Pipes/slippibot" + str(port)
            if not os.path.isdir(str(Path.home()) + "/.config/SlippiOnline/Pipes/"):
                os.mkdir(str(Path.home()) + "/.config/SlippiOnline/Pipes/")
            return str(Path.home()) + "/.config/SlippiOnline/Pipes/slippibot" + str(port)
        return self._get_dolphin_home_path() + "/Pipes/slippibot" + str(port)

    def __fixframeindexing(self, gamestate):
        """ Melee's indexing of action frames is wildly inconsistent.
            Here we adjust all of the frames to be indexed at 1 (so math is easier)"""
        for _, player in gamestate.player.items():
            if player.action.value in self.zero_indices[player.character.value]:
                player.action_frame = player.action_frame + 1

    def __fixiasa(self, gamestate):
        """ The IASA flag doesn't set or reset for special attacks.
            So let's just set IASA to False for all non-A attacks.
        """
        for _, player in gamestate.player.items():
            # Luckily for us, all the A-attacks are in a contiguous place in the enums!
            #   So we don't need to call them out one by one
            if player.action.value < Action.NEUTRAL_ATTACK_1.value or player.action.value > Action.DAIR.value:
                player.iasa = False
