# -*- coding: utf-8 -*-
import terrariumLogging
logger = terrariumLogging.logging.getLogger(__name__)

import threading
import time
import datetime
import os
import signal
import psutil
import subprocess
import re
import json
import pyfiglet
import copy

import gettext
# Loading translations
gettext.install('terrariumpi', 'locales/')

from pathlib import Path
from gevent import sleep
from packaging.version import Version
from pyfancy.pyfancy import pyfancy

from pony import orm
from terrariumDatabase import init as init_db, db, Setting, Sensor, Relay, RelayHistory, Button, Webcam, Enclosure
from terrariumWebserver import terrariumWebserver
from terrariumCalendar import terrariumCalendar
from terrariumUtils import terrariumUtils
from terrariumEnclosure import terrariumEnclosure
from terrariumArea import terrariumArea

from weather import terrariumWeather, terrariumWeatherException
from hardware.sensor import terrariumSensor, terrariumSensorLoadingException
from hardware.relay import terrariumRelay, terrariumRelayLoadingException
from hardware.button import terrariumButton, terrariumButtonLoadingException
from hardware.webcam import terrariumWebcam, terrariummWebcamLoadingException

# REMOVE????
from terrariumEnvironment import terrariumEnvironment
# REMOVE????
from terrariumNotification import terrariumNotification

def N_(message):
  return message

class terrariumEngine(object):
  __ENGINE_LOOP_TIMEOUT          = 30.0 # in seconds
  __VERSION_UPDATE_CHECK_TIMEOUT = 1    # in days

  def __init__(self, version):
    starttime = time.time()
    # Default system units
    self.units = {N_('temperature') : 'C',
                  N_('distance')    : 'cm',
                  N_('altitude')    : 'cm',
                  N_('pressure')    : 'hPa',
                  N_('humidity')    : '%',
                  N_('moisture')    : '%',
                  N_('conductivity'): 'mS',
                  N_('ph')          : 'pH',
                  N_('light')       : 'lux',
                  N_('uva')         : 'µW/cm²',
                  N_('uvb')         : 'µW/cm²',
                  N_('uvi')         : '',
                  N_('fertility')   : 'µS/cm',
                  N_('co2')         : 'ppm',
                  N_('volume')      : 'L',
                  N_('watertank')   : 'L',
                  N_('windspeed')   : 'kmh',
                  N_('water_flow')  : 'L/m',
                  N_('wattage')     : 'W',
                 }

    self.__engine = {'exit' : threading.Event(), 'thread' : None, 'logtail' : None, 'too_late': 0}

    self.version = version
    self.latest_version = None
    init_db(self.version)

    self.running = True

    # Make the first round of logging visible to the console, as this is the startup
    old_log_level = terrariumLogging.logging.getLogger().handlers[0].level
    terrariumLogging.logging.getLogger().handlers[0].setLevel(terrariumLogging.logging.INFO)
    logger.info(f'Starting TerrariumPI {self.version} ...')

    # Add 'empty' weather object
    self.weather = None

    # Load settings. This will also load the weather data if available
    self.load_settings()

    # Notification system
    self.notification = terrariumNotification()
    self.notification.engine = self
    self.notification.load_services()

#    self.notification.set_profile_image(self.get_profile_image())

    # Load Webserver, as we need it for websocket communication (even when the webserver is not yet started)
    self.webserver = terrariumWebserver(self)

    # Load notification......

    # Loading calendar
    self.calendar = terrariumCalendar()

    # Loading the sensors
    start = time.time()
    logger.info('Loading existing sensors from database.')
    self.__load_existing_sensors()

    logger.info('Scanning for new sensors ...')
    self.scan_new_sensors()
    logger.info(f'Loaded {len(self.sensors)} sensors in {time.time()-start:.2f} seconds.')

    # Loading relays
    start = time.time()
    logger.info('Loading existing relays from database.')
    self.__load_existing_relays()

    logger.info('Scanning for new relays ...')
    self.scan_new_relays()
    logger.info(f'Loaded {len(self.relays)} relays in {time.time()-start:.2f} seconds.')

    # Loading buttons....
    start = time.time()
    logger.info('Loading existing buttons from database.')
    self.__load_existing_buttons()
    logger.info(f'Loaded {len(self.buttons)} buttons in {time.time()-start:.2f} seconds.')

    # Loading webcams
    start = time.time()
    logger.info('Loading existing webcams from database.')
    self.__load_existing_webcams()
    logger.info(f'Loaded {len(self.webcams)} webcams in {time.time()-start:.2f} seconds.')

    # Loading enclosures and areas
    start = time.time()
    logger.info('Loading existing enclosures from database.')
    self.__load_existing_enclosures()
    logger.info(f'Loaded {len(self.enclosures)} enclosures in {time.time()-start:.2f} seconds.')

    self.environment = None



    logger.info(f'TerrariumPI is up and running at address: http://{self.settings["host"]}:{self.settings["port"]} in {time.time()-starttime:.2f} seconds.')
    # Return console logging back to 'normal'
    terrariumLogging.logging.getLogger().handlers[0].setLevel(old_log_level)
    self.__engine['logtail'] = threading.Thread(target=self.__log_tailing)
    self.__engine['thread']  = threading.Thread(target=self.__engine_loop)

    self.__engine['logtail'].start()
    self.__engine['thread'].start()

    # Start the webserver. This will be ending by pressing Ctrl-C or sending kill -INT {PID}
    self.webserver.start()

  def restart(self):
    def sigint_process():
      terrariumUtils.get_script_data(f'kill -INT {os.getpid()}')

    threading.Timer(2,sigint_process).start()
    logger.warning(f'Restarting TerrariumPI {self.settings["version"]} now!')
    return True

  def reboot(self):
    def sigint_process():
      terrariumUtils.get_script_data(f'kill -INT {os.getpid()} && sleep 10 && sudo reboot')

    threading.Timer(2,sigint_process).start()
    logger.warning(f'Rebooting TerrariumPI {self.settings["version"]} now!')
    return True

  def shutdown(self):
    def sigint_process():
      terrariumUtils.get_script_data(f'kill -INT {os.getpid()} && sleep 10 && sudo shutdown')

    threading.Timer(2,sigint_process).start()
    logger.warning(f'Shutting down TerrariumPI {self.settings["version"]} now!')
    return True

  @property
  def available_languages(self):
    languages = []
    for language_file in Path('locales/').glob('**/*.po'):
      language = language_file.read_text()
      language_code = re.search('^\"Language: (?P<language>[^<\"]+)', language, re.MULTILINE).group('language').strip().replace('\\n','').strip()
      language_name = re.search('^\"Language-Team: (?P<language>[^<\"]+)', language, re.MULTILINE).group('language').strip().replace('\\n','').strip()

      if '' == language_name or 'none' == language_name:
        language_name = language_code

      languages.append({'id' : language_code, 'name' : language_name})

    return sorted(languages, key=lambda l: l['name'])

  # -=NEW=-
  def load_settings(self):
    start = time.time()
    logger.debug('Loading settings')
    # Store settings first locally, and replace the active settings with the new settings
    settings = {}
    with orm.db_session():
      for setting in Setting.select():
        if terrariumUtils.is_float(setting.value):
          settings[setting.id] = float(setting.value)
        elif setting.value.lower() in ['true','false','on','off','1','0']:
          settings[setting.id] = terrariumUtils.is_true(setting.value)
        else:
          settings[setting.id] = setting.value

        logger.debug(f'Loaded setting: {setting.id} with value: {setting.value}')

    settings['exclude_ids'] = [] if '' == settings['exclude_ids'] else settings['exclude_ids'].split(',')
    # Force portnumer to an int.....
    settings['port']        = int(settings['port'])

    # Set meross login into the current bash environment
    cmd = 'grep -i serial /proc/cpuinfo'
    salt = terrariumUtils.get_script_data(cmd).decode().strip()
    salt = salt.split(':')[1].strip()

    os.environ['SALT']            = salt
    os.environ['MEROSS_EMAIL']    = settings['meross_cloud_username']
    os.environ['MEROSS_PASSWORD'] = settings['meross_cloud_password']

    # Add some extra non DB settings
    settings['version'] = self.version

    # Load the languages. Make a copy of the original, so we can reuse this value easier.
    settings['languages'] = copy.copy(self.available_languages)

    # Load device information
    try:
      settings['device'] = re.search(r'Model\s+:\s+(?P<device>.*)', Path('/proc/cpuinfo').read_text()).group('device')
    except Exception as ex:
      logger.debug(f'Error getting Pi info: {ex}')
      settings['device'] = 'Unknown'

    # Custom favicon
    favicon = Path('media/favicon.ico')
    settings['favicon'] = '/media/favicon.ico' if favicon.exists() else '/favicon.ico'

    # Set unit values
    self.units['temperature'] = ('C' if 'celsius' == settings['temperature_indicator'] else ( 'F' if 'fahrenheit' == settings['temperature_indicator'] else 'K' ))
    self.units['heating']     = self.units['temperature']
    self.units['cooling']     = self.units['temperature']
    self.units['distance']    = ('cm' if 'cm' == settings['distance_indicator'] else 'inch')
    self.units['altitude']    = self.units['distance']
    self.units['volume']      = ('L' if 'l' == settings['water_volume_indicator'] else ( 'UKGall' if 'ukgall' == settings['water_volume_indicator'] else 'USGall' ))
    self.units['water_flow']  = self.units['volume'] + '/m'
    self.units['watertank']   = self.units['volume']

    if 'km/h' == settings['wind_speed_indicator']:
      if 'cm' == settings['distance_indicator']:
        self.units['windspeed'] = 'km/h'
      elif 'inch' == settings['distance_indicator']:
        self.units['windspeed'] = 'm/h'

    elif 'm/s' == settings['wind_speed_indicator']:
      if 'cm' == settings['distance_indicator']:
        self.units['windspeed'] = 'm/s'
      elif 'inch' == settings['distance_indicator']:
        self.units['windspeed'] = 'i/f'

    elif 'beaufort' == settings['wind_speed_indicator']:
      self.units['windspeed'] = 'Bf'
      # https://stackoverflow.com/questions/60001991/how-to-convert-windspeed-between-beaufort-scale-and-m-s-and-vice-versa-in-javasc

    # Replace active settings with the new settings
    self.settings = settings
    logger.info(f'Loaded {len(settings)} settings in {time.time()-start:.2f} seconds.')

    # Loading active language
    gettext.translation('terrariumpi', 'locales/', languages=[self.settings['language']]).install(True)

    # Loading weather
    if 'weather_source' in self.settings:
      if '' != self.settings['weather_source']:
        logger.info(f'Loading weather data from source {self.settings["weather_source"]}')
        self.weather = terrariumWeather(self.settings['weather_source'])
      elif self.weather is not None:
        logger.info(f'Updating weather source data to {self.settings["weather_source"]}')
        self.weather.address = self.settings['weather_source']

  # -=NEW=-
  def __update_checker(self):
    # Set the initial update to 1970 if this is the first update check.
    # Else the attribute __last_update_check is known
    try:
      assert self.__last_update_check
    except Exception:
      self.__last_update_check = datetime.datetime.fromtimestamp(0)

    if datetime.datetime.now() - self.__last_update_check > datetime.timedelta(days=terrariumEngine.__VERSION_UPDATE_CHECK_TIMEOUT):
      version_data = terrariumUtils.get_remote_data('https://api.github.com/repos/theyosh/TerrariumPI/releases/latest',json=True)
      if version_data is None:
        logger.warning('Unable to get the latest version information from Github. Will check next round.')
        return False

      self.latest_version = version_data['tag_name']
      update_available = Version(self.version) < Version(self.latest_version)
      self.__last_update_check = datetime.datetime.now()

      return update_available

    return False

  # -=NEW=-
  def add(self, item):
    if isinstance(item, terrariumButton):
      self.buttons[item.id] = item

    elif isinstance(item, terrariumRelay):
      self.relays[item.id] = item

    elif isinstance(item, terrariumSensor):
      self.sensors[item.id] = item

    elif isinstance(item,terrariumWebcam):
      self.webcams[item.id] = item

    elif isinstance(item,terrariumEnclosure):
      self.enclosures[item.id] = item

    elif isinstance(item,terrariumArea):
      self.enclosures[item.enclosure.id].add(item)

    return item

  # -= NEW =-
  def update(self, item, **data):
    update_ok = False

    if issubclass(item, terrariumButton):
      self.buttons[data['id']].address = data['address']
      self.buttons[data['id']].name    = data['name']
      if 'calibration' in data:
        self.buttons[data['id']].calibrate(data['calibration'])

      update_ok = True

    elif issubclass(item, terrariumRelay):
      if data['id'] not in self.relays:
        return self.add(item)

      self.relays[data['id']].address = data['address']
      self.relays[data['id']].name    = data['name']

      if self.relays[data['id']].is_dimmer:
        self.relays[data['id']].calibrate(data['calibration'])

      update_ok = True

    elif issubclass(item, terrariumSensor):
      self.sensors[data['id']].address = data['address']
      self.sensors[data['id']].name    = data['name']
      update_ok = True

    elif issubclass(item, terrariumWebcam):
      self.webcams[data['id']].address    = data['address']
      self.webcams[data['id']].name       = data['name']

      self.webcams[data['id']].resolution = (int(data['width']),int(data['height']))
      self.webcams[data['id']].rotation   = data['rotation']
      self.webcams[data['id']].awb        = data['awb']

      update_ok = True

    elif issubclass(item, terrariumEnclosure):
#      print('Update enclosure with data')
#      print(data)

      #self.enclosures[data['id']].setup = data['setup']

      update_ok = True

    elif issubclass(item, terrariumArea):
#      print('Update AREA with data')
#      print(data)

      self.enclosures[data['enclosure']].areas[data['id']].name = data['name']
      self.enclosures[data['enclosure']].areas[data['id']].mode = data['mode']
      self.enclosures[data['enclosure']].areas[data['id']].load_setup(data['setup'])

      update_ok = True


    return update_ok

  # -= NEW =-
  def delete(self, item, id):
    delete_ok = False

    if issubclass(item, terrariumButton):
      self.buttons[id].stop()
      del(self.buttons[id])
      delete_ok = True

    elif issubclass(item, terrariumRelay):
      self.relays[id].stop()
      del(self.relays[id])
      delete_ok = True

    elif issubclass(item, terrariumSensor):
      self.sensors[id].stop()
      del(self.sensors[id])
      delete_ok = True

    elif issubclass(item, terrariumWebcam):
      self.webcams[id].stop()
      del(self.webcams[id])
      delete_ok = True

    return delete_ok

  # Private/internal functions
  # -=NEW=-
  def __load_existing_sensors(self):
    self.sensors = {}

    with orm.db_session():
      for sensor in Sensor.select(lambda s: s.id not in self.settings['exclude_ids']):
        start = time.time()
        if sensor.id not in self.sensors:
          logger.debug(f'Loading {sensor}.')
          try:
            self.add(terrariumSensor(sensor.id, sensor.hardware, sensor.type, sensor.address, sensor.name))
            if 'chirp' == sensor.hardware.lower():
              # We need some moisture calibration for a Chirp sensor
              self.sensors[sensor.id].calibrate(sensor.calibration)

          except terrariumSensorLoadingException as ex:
            logger.error(f'Error loading {sensor} with error: {ex}.')
            continue

        else:
          logger.debug(f'Updated already loaded {sensor}.')
          # Update existing sensor with new address
          self.sensors[sensor.id].address = sensor.address

        # Take a measurement from the sensor
        value = self.sensors[sensor.id].update()
        if value is None:
          logger.warning(f'{sensor} had problems reading a new value during startup in {time.time()-start:.2f} seconds. Will be updated in the next round.')

        elif not sensor.limit_min <= value <= sensor.limit_max:
          logger.warning(f'Measurement for sensor {sensor} of {value:.2f}{self.units[sensor.type]} is outside valide range {sensor.limit_min:.2f}{self.units[sensor.type]} to {sensor.limit_max:.2f}{self.units[sensor.type]} during startup in {time.time()-start:.2f} seconds. Will be updated in the next round.')

        else:
          # Store the new measurement value in the database
          sensor.update(value)
          logger.info(f'Loaded sensor {sensor} with value {value:.2f}{self.units[sensor.type]} in {time.time()-start:.2f} seconds.')

  # -=NEW=-
  def scan_new_sensors(self):
    for sensor in terrariumSensor.scan_sensors():
      if sensor.id not in self.settings['exclude_ids'] and sensor.id not in self.sensors:
        start = time.time()
        logger.debug(f'Found new sensor {sensor}')
        with orm.db_session():
          # Store new sensor in database
          new_sensor = Sensor(
            id        = sensor.id,
            hardware  = sensor.HARDWARE,
            type      = sensor.sensor_type,
            name      = sensor.name,
            address   = sensor.address
          )

          # Create a new sensordata entry, so we have at least one sensor value
          value = sensor.update()
          new_sensor.update(value)

        # Store the hardware sensor in memory, so we can benefit from the shared cached data for sensors with multiple sensor types
        self.add(sensor)

        logger.info(f'Added new sensor {new_sensor} to database with value {value:.2f}{self.units[sensor.type]} in {time.time()-start:.2f} seconds.')
      else:
        reason = 'excluded' if sensor.id in self.settings['exclude_ids'] else 'already loaded'
        logger.debug(f'Ignored sensor {sensor} because it is {reason}.')

  # -= NEW =-
  def _update_sensors(self):
    with orm.db_session():
      for sensor in Sensor.select(lambda s: s.id in self.sensors.keys() and not s.id in self.settings['exclude_ids']):
        current_value = sensor.value
        start = time.time()
        new_value = self.sensors[sensor.id].update(self.sensors[sensor.id].erratic > 0)
        measurement_time = time.time() - start
        if new_value is None:
          logger.warning(f'Could not take a new measurement from sensor {sensor}. Tried for {measurement_time:.2f} seconds. Skipping this update.')
          continue

        # Convert some values like temperature and distance ...
        if 'temperature' == sensor.type.lower():
          if 'fahrenheit' == self.settings['temperature_indicator']:
            new_value = terrariumUtils.to_fahrenheit(new_value)
          elif 'kelvin' == self.settings['temperature_indicator']:
            new_value = terrariumUtils.to_kelvin(new_value)

        elif 'distance' == sensor.type.lower():
          if 'inch' == self.settings['distance_indicator']:
            new_value = terrariumUtils.to_inches(new_value)

        # We have a valid reading from the hardware sensor. Now increase/decrease with the offset
        new_value += sensor.offset

        if not sensor.limit_min <= new_value <= sensor.limit_max:
          logger.error(f'Measurement for sensor {sensor} of {new_value:.2f}{self.units[sensor.type]} is outside valide range {sensor.limit_min:.2f}{self.units[sensor.type]} to {sensor.limit_max:.2f}{self.units[sensor.type]}. Skipping this update.')
          continue

        if current_value is not None and sensor.max_diff != 0 and abs(current_value - new_value) > sensor.max_diff:
          self.sensors[sensor.id].erratic += 1
          if self.sensors[sensor.id].erratic < 5:
            logger.warning(f'Sensor {sensor} has an erratic({self.sensors[sensor.id].erratic}) measurement of value {new_value:.2f}{self.units[sensor.type]} compared to old value {current_value:.2f}{self.units[sensor.type]}. The difference of {abs(current_value - new_value):.2f}{self.units[sensor.type]} is more than max allowed difference of {sensor.max_diff:.2f}{self.units[sensor.type]} and will be ignored.')
            new_value = current_value
          else:
            logger.warning(f'After {self.sensors[sensor.id].erratic} erratic measurements the new value {new_value:.2f}{self.units[sensor.type]} is promoted to the current value for sensor {sensor}.')
            self.sensors[sensor.id].erratic = 0
        else:
          self.sensors[sensor.id].erratic = 0

        if new_value is not None:
          sensor.update(new_value)
          db_time = (time.time() - start) - measurement_time
          self.webserver.websocket_message('gauge_update' , {'id' : sensor.id, 'value' : new_value})

          logger.info(f'Updated sensor {sensor} with new value {new_value:.2f}{self.units[sensor.type]} in {measurement_time+db_time:.2f} seconds.')
          logger.debug(f'Updated sensor {sensor} with new value {new_value:.2f}{self.units[sensor.type]}. M: {measurement_time:.2f} sec, DB:{db_time:.2f} sec.')

    for sensor_type, avg_data in self.sensor_averages.items():
      self.webserver.websocket_message('gauge_update', { 'id' : f'avg_{sensor_type}', 'value' : avg_data['value']})

  # TODO: Use db avg functions
  @property
  def sensor_averages(self):
    start = time.time()
    data = {}

    with orm.db_session():
      for sensor in Sensor.select(lambda s: s.id in self.sensors.keys() and not s.id in self.settings['exclude_ids']):
        if sensor.value is None:
          continue

        if sensor.type not in data:
          data[sensor.type] = {'value' : 0.0, 'count' : 0.0, 'alarm_min' : 0, 'alarm_max' : 0, 'limit_min' : 0, 'limit_max' : 0}

        data[sensor.type]['value']     += sensor.value
        data[sensor.type]['alarm_min'] += sensor.alarm_min
        data[sensor.type]['alarm_max'] += sensor.alarm_max
        data[sensor.type]['limit_min'] += sensor.limit_min
        data[sensor.type]['limit_max'] += sensor.limit_max
        data[sensor.type]['count']     += 1.0

    averages = {}
    for sensor_type, sensor_data in data.items():
      count = sensor_data['count']
      del(sensor_data['count'])
      averages[sensor_type] = {}
      for part, value in sensor_data.items():
        averages[sensor_type][part] = value / count

    logger.debug(f'Calculated sensor averages in {time.time()-start:.2f} seconds.')
    return averages

  @property
  def sensor_types_loaded(self):
    start = time.time()
    data = []
    with orm.db_session():
      for sensor in Sensor.select(lambda s: s.id in self.sensors.keys() and not s.id in self.settings['exclude_ids']):
        if sensor.type not in data:
          data.append(sensor.type)

    data = [{'id' : sensor_type, 'value' : sensor_type} for sensor_type in data]
    return data

  # -= NEW =-
  def __load_existing_relays(self):
    self.relays = {}

    with orm.db_session():
      # TODO: Fix Meross better!!  and r.hardware != 'meross'
      for relay in Relay.select(lambda r: r.id not in self.settings['exclude_ids']):
        start = time.time()
        if relay.id not in self.relays:
          logger.debug(f'Loading {relay}.')
          try:
            new_relay = self.add(terrariumRelay(relay.id, relay.hardware, relay.address, relay.name, callback=self.callback_relay))
            if relay.is_dimmer and relay.calibration is not None:
              new_relay.calibrate(relay.calibration)

            # Set the relay back to the old state
            last_value = relay.value
            # If we do not have a last value, try to get one from the relay
            if last_value is None:
              # If no state info available, then asume off
              last_value = self.relays[relay.id].update() or terrariumRelay.OFF

            # Restore the state of the relay
            self.relays[relay.id].set_state(last_value, True)
          except terrariumRelayLoadingException as ex:
            logger.error(f'Error loading relay {relay} with error: {ex.message}.')
            continue

        # else:
        #   logger.debug(f'Updated already loaded {relay}.')
        #   # Update existing relay with new address
        #   self.relays[relay.id].address = relay.address

        # Take a measurement from the relay
        value = self.relays[relay.id].update()
        if value is None:
          logger.warning(f'Relay {relay} had problems reading a new value during startup in {time.time()-start:.2f} seconds. Will be updated in the next round.')
        else:
          # Force a the new measurement value in the database
          relay.update(value, True)
          logger.info(f'Loaded relay {relay} value {value:.2f} in {time.time()-start:.2f} seconds.')


  # -= NEW =-
  def scan_new_relays(self):
    for relay in terrariumRelay.scan_relays(callback=self.callback_relay):
      if relay.id not in self.settings['exclude_ids'] and relay.id not in self.relays:
        logger.debug(f'Found new relay {relay}')
        with orm.db_session():
          # Store new relay in database
          new_relay = Relay(
            id        = relay.id,
            hardware  = relay.HARDWARE,
            name      = relay.name,
            address   = relay.address
          )

          # Create a new relaydata entry, so we have at least one relay value
          value = relay.update()
          new_relay.update(value)

        # Store the hardware relay in memory, so we can benefit from the shared cached data for relays with multiple relay types
        self.add(relay)
        logger.info(f'Added new relay {new_relay} to database with current value {value:.2f}.')
      else:
        logger.debug('Ignored relay {} because it is {}.'.format(relay, 'excluded' if relay.id in self.settings['exclude_ids'] else 'already loaded'))


  # -= NEW =-
  def _update_relays(self):
    # Force an update every hour. This will make the graphs work better...
    force_update = int(time.time()) % (60 * 60) <= terrariumEngine.__ENGINE_LOOP_TIMEOUT
    with orm.db_session():
      for relay in Relay.select(lambda r: r.id in self.relays.keys() and not r.id in self.settings['exclude_ids']):
        start = time.time()
        new_value = self.relays[relay.id].update()
        measurement_time = time.time() - start

        if new_value is None:
          logger.warning(f'Could not take a new measurement from relay {relay}. Tried for {measurement_time:.2f} seconds. Skipping this update.')
          continue

        relay.update(new_value,force_update)
        db_time = (time.time() - start) - measurement_time
        self.webserver.websocket_message('relay' , {'id' : relay.id, 'value' : new_value})

        logger.info(f'Updated relay {relay} with new value {new_value:.2f} in {measurement_time+db_time:.2f} seconds.')
        logger.debug(f'Updated relay {relay} with new value {new_value:.2f}. M: {measurement_time:.2f} sec, DB:{db_time:.2f} sec.')

    self.webserver.websocket_message('power_usage_water_flow', self.get_power_usage_water_flow)

  # -= NEW =-
  def toggle_relay(self, relay, action = 'toggle', duration = 0):
    ok = False

    # Convert the action to an int value
    if 'on' == action:
      action = terrariumRelay.ON
    elif 'off' == action:
      action = terrariumRelay.OFF
    elif 'toggle' == action:
      action = terrariumRelay.OFF if relay.value != terrariumRelay.OFF else terrariumRelay.ON
    else:
      action = int(action)

    # Toggle the switch, will return ok = true when succeeded
    ok = self.relays[relay.id].set_state(action)
    return ok

  # -= NEW =-
  def callback_relay(self, relay, state):
    with orm.db_session():
      relay = Relay[relay]
      relay.update(state)
      # And send a websocket update
      self.webserver.websocket_message('relay' , {'id' : relay.id, 'value' : state})
      self.webserver.websocket_message('power_usage_water_flow', self.get_power_usage_water_flow)

  # -= NEW =-
  def __load_existing_buttons(self):
    self.buttons = {}

    with orm.db_session():
      for button in Button.select(lambda b: b.id not in self.settings['exclude_ids']):
        start = time.time()
        if button.id not in self.buttons:
          logger.debug(f'Loading {button}.')
          try:
            new_button = self.add(terrariumButton(button.id, button.hardware, button.address, button.name, self.button_action))
            if button.calibration is not None:
              new_button.calibrate(button.calibration)

          except terrariumButtonLoadingException as ex:
            logger.error(f'Error loading {button} with error: {ex.message}.')
            continue

        else:
          logger.debug(f'Updated already loaded {button}.')
          # Update existing button with new address
          self.buttons[button.id].address = button.address

        # Take a measurement from the button
        value = self.buttons[button.id].update()
        if value is None:
          logger.warning(f'{button} had problems reading a new value during startup in {time.time()-start:.2f} seconds. Will be updated in the next round.')
        else:
          # Store the new measurement value in the database
          button.update(value)
          logger.info(f'Loaded {button} value {value:.2f} in {time.time()-start:.2f} seconds.')

  # -= NEW =-
  def _update_buttons(self):
    # Force an update every hour. This will make the graphs work better...
    force_update = int(time.time()) % (60 * 60) <= terrariumEngine.__ENGINE_LOOP_TIMEOUT
    with orm.db_session():
      for button in Button.select(lambda b: b.id in self.buttons.keys() and not b.id in self.settings['exclude_ids']):
        start = time.time()
        new_value = self.buttons[button.id].update()
        measurement_time = time.time() - start

        if new_value is None:
          logger.warning(f'Could not take a new measurement from {button}. Tried for {measurement_time:.2f} seconds. Skipping this update.')
          continue

        button.update(new_value,force_update)
        db_time = (time.time() - start) - measurement_time

        logger.info(f'Updated {button} with new value {new_value:.2f} in {measurement_time+db_time:.2f} seconds.')
        logger.debug(f'Updated {button} with new value {new_value:.2f}. M: {measurement_time:.2f} sec, DB:{db_time:.2f} sec.')

  # -= NEW =-
  def button_action(self, button, state):
    with orm.db_session():
      button = Button[button]
      button.update(state,True)

      # Update the button state on the button page
      self.webserver.websocket_message('button' , {'id' : button.id, 'hardware' : button.hardware, 'value' : button.value})

      if button.enclosure:
        # This is called a door, because has a link with an enclosure
        # But not sure if this is handy, because of inverse use of open/closed
        status = 'closed' if button.value else 'open'
        self.webserver.websocket_message('door_status' , {'message' : f'Door {button.name} at enclosure {button.enclosure.name} is {status}', 'status' : status})

        # Get a list of all the used doors and their status
        door_status = []
        for door in button.enclosure.doors:
          status = 'closed' if door.value else 'open'
          door_status.append({
            'id' : door.id,
            'name' : door.name,
            'enclosure' : door.enclosure.name,
            'enclosure_id' : str(door.enclosure.id),
            'status' : status,
            'message' : f'Door {door.name} is {status}'
          })

        self.webserver.websocket_message('doors' , door_status)


  # -= NEW =-
  def __load_existing_webcams(self):
    self.webcams = {}

    with orm.db_session():
      for webcam in Webcam.select(lambda w: w.id not in self.settings['exclude_ids']):
        start = time.time()
        if webcam.id not in self.webcams:
          logger.debug(f'Loading {webcam}.')
          #try:
          self.add(terrariumWebcam(webcam.id,
                                    webcam.address,
                                    webcam.name,
                                    int(webcam.width),
                                    int(webcam.height),
                                    webcam.rotation,
                                    webcam.awb))

          #except terrariumWebcamLoadingException as ex:
          #  logger.error(f'Error loading {webcam} with error: {ex.message}.'))
          #  continue

        else:
          logger.debug(f'Updated already loaded {webcam}.')
          # Update existing webcam with new address
          self.webcams[webcam.id].address = webcam.address

        # # Take a shot from the webcam
        relays = [] if webcam.flash is None else [self.relays[relay.id] for relay in webcam.flash if not relay.manual_mode]
        value = self.webcams[webcam.id].update(relays)

        logger.info(f'Loaded {webcam} in {time.time()-start:.2f} seconds.')

  # -= NEW =-
  def _update_webcams(self):
    with orm.db_session():
      for webcam in Webcam.select(lambda w: w.id in self.webcams.keys() and not w.id in self.settings['exclude_ids']):
        start = time.time()

        relays = [] if webcam.flash is None else [self.relays[relay.id] for relay in webcam.flash if not relay.manual_mode]
        self.webcams[webcam.id].update(relays)

        if 'disabled' != webcam.archive['state']:
          # Check archiving/motion settings

          # Check light status
          if 'ignore' != webcam.archive['light']:
            # Default state is that the lights are on....
            current_state = 'on' if webcam.enclosure is None or self.enclosures[webcam.enclosure.id].lights_on else 'off'

            if webcam.archive['light'] != current_state:
              print(f'Webcam {webcam} will not archive based on light state: {current_state} vs {webcam.archive["light"]}')
              continue

          # Check door status
          if 'ignore' != webcam.archive['door']:
            # Default state is that the doors are closed....
            current_state = 'close' if webcam.enclosure is None or self.enclosures[webcam.enclosure.id].door_closed else 'open'

            if webcam.archive['door'] != current_state:
              print(f'Webcam {webcam} will not archive based on door state: {current_state} vs {webcam.archive["door"]}')
              continue

          if 'motion' == webcam.archive['state']:
            self.webcams[webcam.id].motion_capture(webcam.motion['frame'], int(webcam.motion['threshold']), int(webcam.motion['area']), webcam.motion['boxes'])
          else:
            self.webcams[webcam.id].archive(int(webcam.archive['state']))

        logger.info(f'Updated {webcam} in {time.time()-start:.2f} seconds.')

  # -= NEW =-
  def __load_existing_enclosures(self):
    self.enclosures = {}

    with orm.db_session():
      for enclosure in Enclosure.select(lambda e: e.id not in self.settings['exclude_ids']):
        start = time.time()
        if enclosure.id not in self.enclosures:
          logger.debug(f'Loading {enclosure}.')

          # TODO: Sensors should be database entities... so query them when needed in the area itselfs
          new_enclosure = self.add(terrariumEnclosure(
                                      str(enclosure.id),
                                      enclosure.name,
                                      self,
                                      [door.id for door in enclosure.doors],
                                      list(enclosure.areas)))


        else:
          logger.debug(f'Updated already loaded {enclosure}.')
          # Update existing enclosure with new setup....?

        new_enclosure.update()
        logger.info(f'Loaded {enclosure} in {time.time()-start:.2f} seconds.')

  # -= NEW =-
  def _update_enclosures(self):
    with orm.db_session():
      for enclosure in Enclosure.select():
        if str(enclosure.id) not in self.enclosures.keys() or str(enclosure.id) in self.settings['exclude_ids']:
          continue

        start = time.time()
        area_states = self.enclosures[str(enclosure.id)].update()
        for area in enclosure.areas:
          area_state = area_states.get(str(area.id),None)
          if area_state:
            area.state = area_state

        enclosure_data = enclosure.to_dict(with_collections=True, related_objects=True)
        enclosure_data['id']  = str(enclosure.id)

        for area in list(enclosure_data['areas']):
          enclosure_data['areas'].remove(area)

          area = area.to_dict(exclude='enclosure')
          area['id'] = str(area['id'])

          enclosure_data['areas'].append(area)

        for door in list(enclosure_data['doors']):
          enclosure_data['doors'].remove(door)

          door_data = door.to_dict(exclude='enclosure')
          door_data['value'] = door.value

          enclosure_data['doors'].append(door_data)

        for webcam in list(enclosure_data['webcams']):
          enclosure_data['webcams'].remove(webcam)

          webcam_data = webcam.to_dict(exclude='enclosure')

          enclosure_data['webcams'].append(webcam_data)

        self.webserver.websocket_message('enclosure' , enclosure_data)
        measurement_time = time.time() - start

        logger.info(f'Updated {enclosure} in {measurement_time:.2f} seconds.')
        logger.debug(f'Updated {enclosure}. M: {measurement_time:.2f} sec.')

  # -= NEW =-
  def __engine_loop(self):
    logger.info(f'Starting engine updater with {terrariumEngine.__ENGINE_LOOP_TIMEOUT:.2f} seconds interval.')
    # A small sleep here, will make the webinterface start directly. Else we have to wait till the first update run is done :(
    sleep(0.25)
    prev_delay = 0

    while not self.__engine['exit'].is_set():
#    while self.running:
      logger.info(f'Starting a new update round with {len(self.sensors)} sensors, {len(self.relays)} relays, {len(self.buttons)} buttons and {len(self.webcams)} webcams.')
      start = time.time()
      update_threads = []
      # Sensors
      update_threads.append(threading.Thread(target=self._update_sensors))
      # Relays
      update_threads.append(threading.Thread(target=self._update_relays))
      # Buttons
      update_threads.append(threading.Thread(target=self._update_buttons))
      # Webcams
      update_threads.append(threading.Thread(target=self._update_webcams))

      for updater in update_threads:
        updater.start()

      # Version update check
      self.__update_checker()

      # Weather data
      if self.weather is not None:
        self.weather.update()

      # Systemstats (needs weather update)
      self.webserver.websocket_message('systemstats', self.system_stats())

      # Wait till all updates are done before continue
      for updater in update_threads:
        updater.join()

      # Run encouter/environment updates
      self._update_enclosures()

      self.motd()

      duration = time.time() - start
      time_left = terrariumEngine.__ENGINE_LOOP_TIMEOUT - duration

      print(f'[{datetime.datetime.now()}] Wait {time_left} seconds for next round')
      print(f'[{datetime.datetime.now()}] Time left form last round: {prev_delay}')
      print(f'[{datetime.datetime.now()}] Finale timeout: {max(0,time_left-prev_delay)}')

      if time_left > 0.0:
        logger.info(f'Engine update done in {duration:.2f} seconds. Waiting for {time_left:.2f} seconds for the next round.')
#        sleep(max(0,time_left-prev_delay))
        self.__engine['exit'].wait(max(0,time_left-prev_delay))
        prev_delay = 0
        self.__engine['too_late'] = 0
        print(f'[{datetime.datetime.now()}] Done waiting....')
      else:
        self.__engine['too_late'] += 1
        prev_delay = abs(time_left)
        logger.warning(f'Engine update took {duration:.2f} seconds. That is {prev_delay:.2f} seconds short.')
        if self.__engine['too_late'] > 30:
          logger.error(f'Engine can\'t keep up. For {self.__engine["too_late"]} times it could not finish in {terrariumEngine.__ENGINE_LOOP_TIMEOUT} seconds.')

    logger.info('Stopped main engine thread')

  def motd(self):
    # Enable translations
    _ = terrariumUtils.get_translator(self.settings['language'])

    # Default left padding
    padding = 2 * ' '
    # Longest text lines first...
    tmp = self.system_stats()
    system_stats = []
    system_stats.append({
      'title' : _('Up time') + ':',
      'value' : terrariumUtils.format_uptime(tmp['uptime']),
      'alarm' : False
    })

    system_stats.append({
      'title' : _('System load') + ':',
      'value' : str(tmp['load']['absolute'])[1:-1],
      'alarm' : tmp['load']['absolute'][0] > 1.0
    })

    system_stats.append({
      'title' : _('CPU Temperature') + ':',
      'value' : f'{tmp["cpu_temperature"]} {self.units["temperature"]}',
      'alarm' : tmp["cpu_temperature"] > 50
    })

    system_stats.append({
      'title' : _('Storage') + ':',
      'value' : f'{terrariumUtils.format_filesize(tmp["storage"]["used"])}({ tmp["storage"]["used"] / tmp["storage"]["total"] * 100:.2f}%) used of total {terrariumUtils.format_filesize(tmp["storage"]["total"])}',
      'alarm' : False
    })

    system_stats.append({
      'title' : _('Memory') + ':',
      'value' : f'{terrariumUtils.format_filesize(tmp["memory"]["used"])}({ tmp["memory"]["used"] / tmp["memory"]["total"] * 100:.2f}%) used of total {terrariumUtils.format_filesize(tmp["memory"]["total"])}',
      'alarm' : False
    })

    # Get the sensors averages sorted on type name
    tmp = self.sensor_averages
    averages = []
    for avg_type in sorted(tmp.keys()):
      averages.append({
        'title' : _('average {sensor_type}').format(sensor_type=_(avg_type)).capitalize() + ':',
        'value' : f'{tmp[avg_type]["value"]:.2f}',
        'unit'  : self.units[avg_type],
        'alarm' : not tmp[avg_type]['alarm_min'] <= tmp[avg_type]['value'] <= tmp[avg_type]['alarm_max']
      })

    # Get the lengths of all the texts for the text alignment
    avg_title_length    = max([len(line['title']) for line in averages])
    avg_value_length    = max([len(line['value']) for line in averages])
    avg_unit_length     = max([len(line['unit'])  for line in averages])
    system_title_length = max([len(line['title']) for line in system_stats])
    system_value_length = max([len(line['value']) for line in system_stats])

    # Start creating the average MOTD lines
    motd_averages = ''
    for line_counter in range(max([len(averages),len(system_stats)])):
      empty_avg   = line_counter >= len(averages)
      empty_stats = line_counter >= len(system_stats)
      # Add default padding
      motd_averages += padding

      # Add the average title name
      motd_averages += avg_title_length * ' ' if empty_avg else averages[line_counter]['title'].ljust(avg_title_length,' ')
      # Add the average value with or without alarm color
      motd_averages += ' '
      motd_averages += avg_value_length * ' ' if empty_avg else ((avg_value_length - len(averages[line_counter]['value'])) * ' ') + (averages[line_counter]['value'] if not averages[line_counter]['alarm'] else pyfancy().yellow(averages[line_counter]['value']).get())
      # Add the average unit type with or without alarm color
      motd_averages += ' '
      motd_averages += avg_unit_length  * ' ' if empty_avg else (averages[line_counter]['unit'] if not averages[line_counter]['alarm'] else pyfancy().yellow(averages[line_counter]['unit']).get()) + ((avg_unit_length - len(averages[line_counter]['unit'])) * ' ')

      # Add system stats
      motd_averages += '  '
      motd_averages += '' if empty_stats else system_stats[line_counter]['title'].ljust(system_title_length,' ')
      # Add the system value with or without alarm color
      motd_averages += ' '
      motd_averages += '' if empty_stats else (system_stats[line_counter]['value'] if not system_stats[line_counter]['alarm'] else pyfancy().yellow(system_stats[line_counter]['value']).get())

      motd_averages += '\n'

    # Generate ascii art title in color
    figlet = pyfiglet.Figlet(font='doom')
    title = self.settings['title']
    red_color = 0

    if 'PI' in title:
      split_pos   = title.find('PI')

      # This is the red part ( PI )
      title_part2 = figlet.renderText(title[split_pos:split_pos+2]).split('\n')

      if split_pos == 0:
        title_part1 = [''] * len(title_part2)
        title_part3 = figlet.renderText(title[split_pos+2:]).split('\n')

      elif split_pos == len(title) -2:
        title_part1 = figlet.renderText(title[0:split_pos]).split('\n')
        title_part3 = [''] * len(title_part2)

      else:
        title_part1 = figlet.renderText(title[0:split_pos]).split('\n')
        title_part3 = figlet.renderText(title[split_pos+2:]).split('\n')

    else:
      title_part1 = figlet.renderText(title).split('\n')
      title_part2 = [''] * len(title_part1)
      title_part3 = [''] * len(title_part1)

    # Get the lengths of all the texts for the text alignment (befor adding colors, as they are counted for the length also)
    max_title_length = max([len(line) for line in title_part1]) + max([len(line) for line in title_part2]) + max([len(line) for line in title_part3]) + 1
    max_line_length = avg_title_length + avg_value_length + avg_unit_length + system_title_length + system_value_length + 4
    title_padding = int((max_line_length - max_title_length) / 3) * ' '

    motd_title = ''
    for counter, __dummy in enumerate(title_part2):
      if '' == title_part1[counter].strip() and '' == title_part2[counter].strip() and '' == title_part3[counter].strip():
        continue

      motd_title += padding + title_padding
      motd_title += pyfancy().green(title_part1[counter]).get()
      motd_title += pyfancy().red(title_part2[counter]).get()
      motd_title += pyfancy().green(title_part3[counter]).get()
      motd_title += '\n'

    # Current version and update message
    update_available = Version(self.version) < Version(self.latest_version)
    motd_version = f'Version: {self.version}' + (f' / {self.latest_version}' if update_available else '')
    version_length = len(motd_version)
    version_padding = (int(max_line_length*0.66) - version_length) * ' '

    if update_available:
      motd_version = pyfancy().yellow(motd_version).get()

    motd_version = padding + version_padding + motd_version + '\n'

    if update_available:
      motd_version += padding + 'New version available: https://github.com/theyosh/TerrariumPI/releases' + '\n'

    # Relays
    relay_lines = [[],[],[]]

    relay_title_length = 0
    relay_power_length = 0
    relay_flow_length  = 0
    relay_title_padding = 1

    relay_averages = {'power': {'current' : 0, 'max' : 0}, 'flow': {'current' : 0, 'max' : 0}}
    with orm.db_session():
      for relay in Relay.select(lambda r: r.id in self.relays.keys() and not r.id in self.settings['exclude_ids']):
        relay_averages['power']['current'] += relay.current_wattage
        relay_averages['power']['max']     += relay.wattage

        relay_averages['flow']['current']  += relay.current_flow
        relay_averages['flow']['max']      += relay.flow

        if not relay.is_on:
          continue

        relay_title = f'{padding}{relay.name}'
        if relay.is_dimmer:
          relay_title += f' ({relay.value:.0f}%)'

        relay_lines[0].append(f'{relay_title}  ')
        relay_lines[1].append(f'{relay.current_wattage:.2f} Watt')
        relay_lines[2].append(f'{relay.current_flow:.2f} L/m')

    current_watt = relay_averages['power']['current']
    max_watt     = relay_averages['power']['max']

    current_flow = relay_averages['flow']['current']
    max_flow     = relay_averages['flow']['max']

    relays_active = len(relay_lines[0])
    motd_relays = ''

    if len(relay_lines[0]) > 0:
      relay_title_length = max([len(title) for title in relay_lines[0]])
      relay_power_length = max([len(title) for title in relay_lines[1]])
      relay_flow_length  = max([len(title) for title in relay_lines[2]])

      for counter in range(len(relay_lines[0])):
        motd_relays += relay_lines[0][counter].ljust(relay_title_length,' ') + ' ' + relay_lines[1][counter].rjust(relay_power_length,' ') + ' ' + relay_lines[2][counter].ljust(relay_flow_length,' ') + '\n'

      motd_relays = padding + ((relay_title_length + relay_power_length + relay_flow_length) * '-') + '\n' + motd_relays

      relay_title_left = len(f'Current active relays {relays_active}/{len(self.relays)}')
      relay_title_right = len(f'{current_watt:.2f}/{max_watt:.2f} Watt, {current_flow:.2f}/{max_flow:.2f} L/m')
      relay_title_padding = (relay_title_length + relay_power_length + relay_flow_length) - (relay_title_left + relay_title_right) - 2

      # Add colors to the values
      if relays_active > 0:
        relays_active = pyfancy().green(f'{relays_active}').get()
        current_watt = pyfancy().green(f'{current_watt:.2f}').get()
        current_flow = pyfancy().blue(f'{current_flow:.2f}').get()

    motd_relays = f'{padding}Current active relays ({relays_active}/{len(self.relays)})' + (relay_title_padding * ' ') + f'{current_watt}/{max_watt:.2f} Watt, {current_flow}/{max_flow:.2f} L/m' + '\n' + motd_relays

    #if self.__engine['too_late'] > 30:
    motd_relays += '\n'
    motd_relays += (2 * padding) + pyfancy().red(f'Engine can\'t keep up. For {self.__engine["too_late"]} times it could not finish in {terrariumEngine.__ENGINE_LOOP_TIMEOUT} seconds.').get()
    motd_relays += '\n'

    # Last update line
    last_update = _('last update').capitalize()
    motd_last_update = (3 * padding) + pyfancy().blue(f'{last_update}: {datetime.datetime.now():%A, %d-%m-%Y %H:%M:%S}').get()

    motd_file = Path('motd.sh')
    with motd_file.open('w') as motdfile:
      motdfile.write('#!/bin/bash\n')
      motdfile.write('tput reset\n')
      motdfile.write('echo "')
      motdfile.write(motd_title.replace('`','\`') + '\n')
      motdfile.write(motd_version + '\n')
      motdfile.write(motd_averages + '\n')
      motdfile.write(motd_relays + '\n')
      motdfile.write(motd_last_update + '\n')
      motdfile.write('"')

    motd_file.chmod(0o755)

  # -= NEW =-
  def __log_tailing(self):
    logger.info('Starting log tailing.')
    with subprocess.Popen(['tail','-F',terrariumLogging.logging.getLogger().handlers[1].baseFilename],stdout=subprocess.PIPE,stderr=subprocess.PIPE, text=True) as self.__logtail_process:
      for line in self.__logtail_process.stdout:
        self.webserver.websocket_message('logfile_update' , line.strip())

    logger.info('Stopped log tailing.')

  # -= NEW =-
  def stop(self):
    terrariumLogging.logging.getLogger().handlers[0].setLevel(terrariumLogging.logging.INFO)
    logger.info(f'Stopping TerrariumPI {self.version} ...')

    self.running = False

    # Stop engine processing first....
    # print('Trigger exit event')
    # print(self.__engine['exit'])
    # print(dir(self.__engine['exit']))

    self.__engine['exit'].set()
    # print('Exit event done')

    # # Wait till the engine is done, when it was updating the sensors
    # print('Stop logtail process')
    self.__logtail_process.terminate()
    # print('Logtail process is done')

    # print('Wait on the engine to stop...')
    # print(self.__engine['thread'])
    # print(f' Thread alive: {self.__engine["thread"].is_alive()}')
    # print(f' Thread daemon: {self.__engine["thread"].isDaemon()}')
    # print(dir(self.__engine['thread']))

    self.__engine['thread'].join()
#    print('Engine stopped...')
#    print('Wait on logtail to stop....')
    self.__engine['logtail'].join()
#    print('Logtail thread is stopped')

    for enclosure in self.enclosures:
      self.enclosures[enclosure].stop()
      logger.info(f'Stopped {self.enclosures[enclosure]}')

    for button in self.buttons:
      self.buttons[button].stop()
      logger.info(f'Stopped {self.buttons[button]}')

    for sensor in self.sensors:
      self.sensors[sensor].stop()
      logger.info(f'Stopped {self.sensors[sensor]}')

    for relay in self.relays:
      self.relays[relay].stop()
      logger.info(f'Stopped {self.relays[relay]}')

    for webcam in self.webcams:
      self.webcams[webcam].stop()
      logger.info(f'Stopped {self.webcams[webcam]}')

    self.notification.stop()
#    print('Totally stopped TerrariumPI')

  def replace_hardware_calender_event(self,switch_id,device,reminder_amount,reminder_period):
    # Two events:
    # 1. When it happend
    # 2. Reminder for next time

    current_time = datetime.date.today()
    switch = self.power_switches[switch_id]
    switch.set_last_hardware_replacement()
    self.config.save_power_switch(switch.get_data())
    self.calendar.create_event(switch_id,
                               '{} hardware replacement'.format(switch.get_name()),
                               'Replaced \'{}\' at power switch {}'.format(device,switch.get_name()),
                               None,
                               current_time)

    reminder = None
    try:
      if 'days' == reminder_period:
        reminder = datetime.timedelta(days=int(reminder_amount))
      elif 'weeks' == reminder_period:
        reminder = datetime.timedelta(days=(int(reminder_amount) * 7))
      elif 'months' == reminder_period:
        reminder = datetime.timedelta(days=(int(reminder_amount) * 30))
      elif 'years' == reminder_period:
        reminder = datetime.timedelta(days=(int(reminder_amount) * 365))
    except Exception as ex:
      print(ex)

    if reminder is not None:
      current_time += reminder
      self.calendar.create_event(switch_id,
                                 'Reminder {} hardware replacement'.format(switch.get_name()),
                                 'Replace \'{}\' at power switch {}'.format(device,switch.get_name()),
                                 None,
                                 current_time)

  # End Calendar part

  def get_audio_playing(self,socket = False):
    data = self.__audio_player.get_current_state()

    if socket:
      self.__send_message({'type':'player_indicator','data': data})
    else:
      return data
  # End audio part

  # Environment part
  def get_environment(self, parameters = [], socket = False):
    filter = None
    if len(parameters) > 0 and parameters[0] is not None:
      filter = parameters[0]

    data = self.environment.get_data()

    if filter is not None and filter in data:
      data = { filter : data[filter]}

    if socket:
      self.__send_message({'type':'environment','data':data})
    else:
      return { 'environment' : data }

  def get_environment_config(self):
    return self.environment.get_config()

  def set_environment_config(self,data):
    self.environment.load_environment(data)
    return self.config.save_environment(self.environment.get_config())
  # End Environment part


  # Profile part
  def get_profile_config(self):
    data = self.config.get_profile()
    if os.path.isfile('description.txt'):
      with open('description.txt', 'r') as description_file:
        data['description'] = description_file.read()

    return data


  # Notifications part
  def get_notifications_config(self):
    return self.notification.get_config()

  def set_notifications(self,data):
    return self.notification.set_config(data)
  # End notifications part

  # System functions part
  # -= NEW =-
  def authenticate(self, username, password):
    return username == self.settings.get('username', None) and terrariumUtils.check_password(password, self.settings.get('password', None))

  # -= NEW =-
  def system_stats(self):
    start = time.time()
    storage = psutil.disk_usage('/')
    memory =  psutil.virtual_memory()
    # Reading temperature through psutil results in a very high load in combination with gevent.... Just reading from disk is way faster.....
    # This is a Raspberry Pi ONLY solution
    cpu_temp = float(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip()) / 1000.0
    data =  {
      'uptime': (datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())).total_seconds(),
      'load' : {
        'percentage' : [x / psutil.cpu_count() * 100 for x in psutil.getloadavg()],
        'absolute' : psutil.getloadavg()
      },
      'cpu_temperature' : cpu_temp,
      'memory' : {'total' : memory.total, 'used':memory.total - memory.available,'free':memory.available},
      'storage' : {'total' : storage.total, 'used':storage.used,'free':storage.free},
      'is_day' : True if self.weather is None else self.weather.is_day
    }
    logger.debug('Loaded system stats {} seconds.'.format(time.time()-start))
    return data

  # -= NEW =-
  @property
  def get_power_usage_water_flow(self):
    data = {'power' : {'current' : float(self.settings['pi_wattage']) , 'max' : float(self.settings['pi_wattage']), 'total' : 0, 'costs' : 0, 'duration' : 0},
            'flow'  : {'current' : 0.0 , 'max' : 0.0, 'total' : 0, 'costs' : 0, 'duration' : 0}}

    with orm.db_session():
      for relay in Relay.select(lambda r: r.id in self.relays.keys() and not r.id in self.settings['exclude_ids']):
        data['power']['current'] += relay.current_wattage
        data['power']['max']     += relay.wattage

        data['flow']['current']  += relay.current_flow
        data['flow']['max']      += relay.flow

    total = self.total_power_and_water_usage

    data['power']['duration'] = total['duration']
    # Total power is converted from watt/s in kWh
    data['power']['total'] = total['total_watt'] / 3600.0 / 1000.0
    # Price is intered as cents per kWh
    data['power']['costs'] = data['power']['total'] * self.settings['power_price']

    data['flow']['duration'] = total['duration']
    # Total water flow is in m3 (1000 L)
    data['flow']['total'] = total['total_flow'] / 1000.0
    data['flow']['costs'] = data['flow']['total'] * self.settings['water_price']

    return data

  # -= NEW =-
  @property
  def total_power_and_water_usage(self):
    # We are using total() vs sum() as total() will always return a number. https://sqlite.org/lang_aggfunc.html#sumunc
    with orm.db_session():
      data = db.select(
        """SELECT
             DISTINCT relay,
             TOTAL(total_wattage) AS wattage,
             TOTAL(total_flow)    AS flow,
             IFNULL((JulianDay(MAX(timestamp2)) - JulianDay(MIN(timestamp))) * 24 * 60 * 60,0) AS timestamp
           FROM (
             SELECT
               RH1.relay     as relay,
               RH1.timestamp as timestamp,
               RH2.timestamp as timestamp2,
                 (JulianDay(RH2.timestamp)-JulianDay(RH1.timestamp))* 24 * 60 * 60                        AS duration_in_seconds,
                ((JulianDay(RH2.timestamp)-JulianDay(RH1.timestamp))* 24 * 60 * 60)         * RH1.wattage AS total_wattage,
               (((JulianDay(RH2.timestamp)-JulianDay(RH1.timestamp))* 24 * 60 * 60) / 60.0) * RH1.flow    AS total_flow
             FROM RelayHistory AS RH1
               LEFT JOIN RelayHistory AS RH2
                 ON RH2.relay = RH1.relay
                 AND RH2.timestamp = (SELECT MIN(timestamp) FROM RelayHistory WHERE timestamp > RH1.timestamp AND relay = RH1.relay)
                 WHERE RH1.value > 0)"""
      )

      return {
        'total_watt' : data[0][1],
        'total_flow' : data[0][2],
        'duration'   : data[0][3]
      }