const LOG_LEVEL = [ "FATAL", "ERROR", "WARN", "INFO", "DEBUG" ];

const LOG_SOURCE = [ "ECU", "ESP", "CAN", "ADC", "TIM", "ACC", "LCD", "GPS" ];

const LOG_KEY = {
  "ECU": [ "ECU_BOOT", "ECU_STATE", "ECU_READY", "SD_INIT" ],
  "ESP": [ "ESP_INIT", "ESP_REMOTE", "ESP_RTC_FIX" ],
  "CAN": {
    0: "CAN_INIT",
    1: "CAN_ERR",

    0xA0: "CAN_INV_TEMP_1",
    0xA1: "CAN_INV_TEMP_2",
    0xA2: "CAN_INV_TEMP_3",
    0xA3: "CAN_INV_ANALOG_IN",
    0xA4: "CAN_INV_DIGITAL_IN",
    0xA5: "CAN_INV_MOTOR_POS",
    0xA6: "CAN_INV_CURRENT",
    0xA7: "CAN_INV_VOLTAGE",
    0xA8: "CAN_INV_FLUX",
    0xA9: "CAN_INV_REF",
    0xAA: "CAN_INV_STATE",
    0xAB: "CAN_INV_FAULT",
    0xAC: "CAN_INV_TORQUE",
    0xAD: "CAN_INV_FLUX_WEAKING",

    0xAE: "CAN_INV_FIRMWARE_VER",
    0xAF: "CAN_INV_DIAGNOSTIC",

    0xB0: "CAN_STEERING_WHEEL_ANGLE",

    0x81: "CAN_BMS_CORE",
    0x82: "CAN_BMS_TEMP",
    
    //linear_sensor
    0x83: "CAN_FRONT_LINEAR_L",
    0x84: "CAN_FRONT_LINEAR_R",
    0x85: "CAN_REAR_LINEAR_L",
    0x86: "CAN_REAR_LINEAR_R",
    
    //temp_sensor
    0x87: "CAN_FRONTTIE_TEMP",
    0x88: "CAN_REARTIE_TEMP",
    
    //accel_sensor
    0x89: "CAN_ACCEL",
    
    
   // 0x2B: "CAN_STEERING_WHEEL_ANGLE",
  },
  "ADC": [ "ADC_INIT", "ADC_CPU", "ADC_DIST" ,"ADC_A3"],
  "TIM": [ "TIMER_IC"],
  "ACC": [ "ACC_INIT", "ACC_DATA" ],
  "LCD": [ "LCD_INIT", "LCD_UPDATED" ],
  "GPS": [ "GPS_INIT", "GPS_POS", "GPS_VEC", "GPS_TIME" ],
};

function translate(raw) {
  try {
    let log = {
      timestamp: raw[0] + raw[1] * Math.pow(2, 8) + raw[2] * Math.pow(2, 16) + raw[3] * Math.pow(2, 24),
      datetime: null,
      level: LOG_LEVEL[raw[4]],
      source: LOG_SOURCE[raw[5]],
      key: LOG_KEY[LOG_SOURCE[raw[5]]][raw[6]],
      checksum: raw[7] == ((raw[0] + raw[1] + raw[2] + raw[3] + raw[4] + raw[5] + raw[6] + raw[8] + raw[9] + raw[10] + raw[11] + raw[12] + raw[13] + raw[14] + raw[15]) % 256),
      value: raw[8] + raw[9] * Math.pow(2, 8) + raw[10] * Math.pow(2, 16) + raw[11] * Math.pow(2, 24) + raw[12] * Math.pow(2, 32) + raw[13] * Math.pow(2, 40) + raw[14] * Math.pow(2, 48) + raw[15] * Math.pow(2, 56),
      raw: raw.slice(8),
    }
    

    // validate checksum
    if (!log.checksum) {
      throw new Error('checksum error');
    }

    log.parsed = parse(log);

    return log;
  } catch(e) {
    return e;
  }
}


function parse(log) {
  let parsed;
  let source = log.source;
  let key = log.key;
  let raw = log.raw;
  let value = log.value; 



  switch (source) {
    case 'ECU': {
      switch (key) {
        case "ECU_BOOT": {
          parsed = value ? true : false;
          break;
        }

        case "ECU_STATE": {
          parsed = {
            HV: value & 1 << 0 ? true : false,
            RTD: value & 1 << 1 ? true : false,
            BMS: value & 1 << 2 ? true : false,
            IMD: value & 1 << 3 ? true : false,
            BSPD: value & 1 << 4 ? true : false,

            ERR: value & 1 << 5 ? true : false,
            SD: value & 1 << 6 ? true : false,
            TELEMETRY: value & 1 << 7 ? true : false,
            CAN: value & 1 << 8 ? true : false,
          };
          break;
        }

        case "ECU_READY": {
          parsed = true;
          break;
        }

        case "SD_INIT": {
          parsed = value ? true : false;
          break;
        }

        default: {
          parsed = null;
          break;
        }
      }
      break;
    }
    case 'ESP':{
      switch (key) {
        case "ESP_INIT": {
          parsed = value ? true : false;
          break;
        }

        case "ESP_REMOTE": {
          parsed = value ? true : false;
          break;
        }

        case "ESP_RTC_FIX": {
          parsed = value ? true : false;
          break;
        }

        default: {
          parsed = null;
          break;
        }
      }
      break;
    }
    case 'CAN': {
      switch (key) {
        case "CAN_INIT": {
          parsed = value ? true : false;
          break;
        }
        case "CAN_ERR":{
          parsed = value;
          break;
        }
        case "CAN_INV_TEMP_1": {
          parsed = {
            igbt: {
              a: signed(value & 0xffff, 16) * 0.1,
              b: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
              c: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            },
            gatedriver: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          parsed.igbt.max = parsed.igbt.a > parsed.igbt.b ? (parsed.igbt.a > parsed.igbt.c ? { temperature: parsed.igbt.a, id: "A" } : { temperature: parsed.igbt.c, id: "C" }) : (parsed.igbt.b > parsed.igbt.c ? { temperature: parsed.igbt.b, id: "B" } : { temperature: parsed.igbt.c, id: "C" });
          break;
        }

        case "CAN_INV_TEMP_2": {
          parsed = {
            controlboard: signed(raw[0] + raw[1] * Math.pow(2, 8), 16) * 0.1,
            RTD1: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
            RTD2: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            RTD3: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          break;
        }

        case "CAN_INV_TEMP_3": {
          parsed = {
            coolant: signed(value & 0xffff, 16) * 0.1,
            hotspot: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
            motor: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            torque_shudder: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          break;
        }

        case "CAN_INV_ANALOG_IN": {
          parsed = {
            AIN1: signed((raw[0] + raw[1] * Math.pow(2, 8)) & 0x3ff, 10) * 0.01,
            AIN2: signed((value / Math.pow(2, 10)) & 0x3ff, 10) * 0.01,
            AIN3: signed((value / Math.pow(2, 20)) & 0x3ff, 10) * 0.01,
            AIN4: signed((value / Math.pow(2, 30)) & 0x3ff, 10) * 0.01,
            AIN5: signed((value / Math.pow(2, 40)) & 0x3ff, 10) * 0.01,
            AIN6: signed((value / Math.pow(2, 50)) & 0x3ff, 10) * 0.01,
          };
          break;
        }

        case "CAN_INV_DIGITAL_IN": {
          parsed = {
            DIN1: (value & 0xff) ? true : false,
            DIN2: ((value / Math.pow(2, 8)) & 0xff) ? true : false,
            DIN3: ((value / Math.pow(2, 16)) & 0xff) ? true : false,
            DIN4: ((value / Math.pow(2, 24)) & 0xff) ? true : false,
            DIN5: ((value / Math.pow(2, 32)) & 0xff) ? true : false,
            DIN6: ((value / Math.pow(2, 40)) & 0xff) ? true : false,
            DIN7: ((value / Math.pow(2, 48)) & 0xff) ? true : false,
            DIN8: ((value / Math.pow(2, 56)) & 0xff) ? true : false,
          };
          break;
        }

        case "CAN_INV_MOTOR_POS":{
          parsed = {
            motor_angle: signed(value & 0xffff, 16) * 0.1,
            motor_speed: signed((value / Math.pow(2, 16)) & 0xffff, 16),
            electrical_output_freq: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            delta_resolver_filtered: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          
          break;
        }

        case "CAN_INV_CURRENT": {
          parsed = {
            phaseA: signed(value & 0xffff, 16) * 0.1,
            phaseB: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
            phaseC: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            dc_bus_current: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          break;
        }

        case "CAN_INV_VOLTAGE": {
          parsed = {
            dc_bus_voltage: signed(value & 0xffff, 16) * 0.1,
            output_voltage: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
            VAB_Vd_voltage: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            VBC_Vq_voltage: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          break;
        }

        case "CAN_INV_FLUX": {
          parsed = {
            flux_command: signed(value & 0xffff, 16) * 0.001,
            flux_feedback: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.001,
            Id_feedback: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            Iq_feedback: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          break;
        }

        case "CAN_INV_REF": {
          parsed = {
            ref_1v5: signed(value & 0xffff, 16) * 0.01,
            ref_2v5: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.01,
            ref_5v: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.01,
            ref_12v: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.01,
          };
          break;
        }

        case "CAN_INV_STATE": {
          parsed = {
            vsm_state: raw[0],
            pwm_freq: raw[1],
            inverter_state: raw[2],
            relay_state:raw[3],
            inverter_run_mode: raw[4] & 0x1,
            inverter_active_discharge_state: (raw[4] / Math.pow(2, 5)) & 0b111,
            inverter_command_mode: raw[5] & 0x1,
            inverter_enable_state: raw[6] & 0x1,
            inverter_start_mode_active: (raw[6] / Math.pow(2, 6)) & 0x1,
            inverter_enable_lockout: (raw[6] / Math.pow(2, 7)) & 0x1,
            direction_command: raw[7] & 0x1,
            bms_active: (raw[7] / Math.pow(2, 1)) & 0x1,
            bms_limiting_torque: (raw[7] / Math.pow(2, 2)) & 0x1,
            limit_max_speed: (raw[7] / Math.pow(2, 3)) & 0x1,
            limit_hot_spot: (raw[7] / Math.pow(2, 4)) & 0x1,
            low_speed_limiting: (raw[7] / Math.pow(2, 5)) & 0x1,
            coolant_temperature_limiting: (raw[7] / Math.pow(2, 6)) & 0x1,
          };
          break;
        }

        case "CAN_INV_FAULT": {
          parsed = {
            POST: value & 0xffffffff,
            RUN: (value / Math.pow(2, 32)) & 0xffffffff,
            POST_FAULT_LO: value & 0xffff,
            POST_FAULT_HI: (value / Math.pow(2, 16)) & 0xffff,
            RUN_FAULT_LO: (value / Math.pow(2, 32)) & 0xffff,
            RUN_FAULT_HI: (value / Math.pow(2, 48)) & 0xffff,
          };
          break;
        }

        case "CAN_INV_TORQUE": {
          parsed = {
            commanded_torque: signed(value & 0xffff, 16) * 0.1,
            torque_feedback: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
            power_on_timer: value / Math.pow(2, 32)
          };
          break;
        }

        case "CAN_INV_FLUX_WEAKING": {
          parsed = {
            modulation_index: (value & 0xffff) * 0.01,
            flux_weakening_output: signed((value / Math.pow(2, 16)) & 0xffff, 16) * 0.1,
            Id_command: signed((value / Math.pow(2, 32)) & 0xffff, 16) * 0.1,
            Iq_command: signed((value / Math.pow(2, 48)) & 0xffff, 16) * 0.1,
          };
          break;
        }

        case "CAN_INV_FIRMWARE_VER": {
          parsed = {
            EEPROM_version: value & 0xffff,
            software_version: (value / Math.pow(2, 16)) & 0xfff,
            date_code: (value / Math.pow(2, 32)) & 0xffff,
            date_code_year: (value / Math.pow(2, 48)) & 0xffff,
          };
          break;
        }

        case "CAN_INV_DIAGNOSTIC": {
          parsed = null;
          break;
        }

       // case "CAN_INV_HIGH_SPD_MSG": {
         // parsed = null;
         // break;
      //  }

        case "CAN_BMS_CORE": {
          const failsafe = raw[7] + raw[6] * Math.pow(2, 8);
          parsed = {
            soc: raw[0] * 0.5,
            capacity: raw[1] * 0.1,
            voltage: (raw[3] + raw[2] * Math.pow(2, 8)) * 0.1,
            current: signed(raw[5] + raw[4] * Math.pow(2, 8), 16) * 0.1,
            failsafe: {
              voltage: failsafe & 1 << 0 ? true : false,
              current: failsafe & 1 << 1 ? true : false,
              relay: failsafe & 1 << 2 ? true : false,
              balancing: failsafe & 1 << 3 ? true : false,
              interlock: failsafe & 1 << 4 ? true : false,
              thermistor: failsafe & 1 << 5 ? true : false,
              power: failsafe & 1 << 6 ? true : false,
            }
          };
          break;
        }

        case "CAN_BMS_TEMP": {
          parsed = {
            temperature: {
              max: {
                value: signed(raw[0], 8),
                id: raw[1],
              },
              min: {
                value: signed(raw[2], 8),
                id: raw[3],
              },
            },
            dcl: (raw[5] + raw[4] * Math.pow(2, 8)),
            ccl: (raw[7] + raw[6] * Math.pow(2, 8)),
          };
          break;
        }

        case "CAN_STEERING_WHEEL_ANGLE": {
          parsed = {
            angle: signed(raw[0] + raw[1] * Math.pow(2, 8), 16) * 0.1 + 2.7, // ±780° 범위
            speed: signed(raw[2] + raw[3] * Math.pow(2, 8), 16) * 4 -7168 // 0 to 1,016°/s 범위
          };
          break;
    }
           case "CAN_FRONT_LINEAR_L": {
          const bits = raw[0] + (raw[1] << 8) + (raw[2] << 16) + (raw[3] << 24) ;
          const buffer = new ArrayBuffer(4);
          const view = new DataView(buffer);
          view.setUint32(0, bits, true);
          parsed = {
            lengthMM: view.getFloat32(0, true) - 57,
          };
          break;
        } 
       
        case "CAN_FRONT_LINEAR_R": {
         const bits = raw[0] + (raw[1] << 8) + (raw[2] << 16) + (raw[3] << 24) ;
          const buffer = new ArrayBuffer(4);
          const view = new DataView(buffer);
          view.setUint32(0, bits, true);
          parsed = {
            lengthMM: view.getFloat32(0, true) - 59,
          };
          break;
        } 
       
        case "CAN_REAR_LINEAR_L": {
          const bits = raw[0] + (raw[1] << 8) + (raw[2] << 16) + (raw[3] << 24);
          const buffer = new ArrayBuffer(4);
          const view = new DataView(buffer);
          view.setUint32(0, bits, true);
          parsed = {
            lengthMM: view.getFloat32(0, true) -50 ,
          };
          break;
        } 
       
        case "CAN_REAR_LINEAR_R": {
          const bits = raw[0] + (raw[1] << 8) + (raw[2] << 16) + (raw[3] << 24) ;
          const buffer = new ArrayBuffer(4);
          const view = new DataView(buffer);
          view.setUint32(0, bits, true);
          parsed = {
            lengthMM: view.getFloat32(0, true)  - 50,
          };
          break;
        } 
    
        case "CAN_FRONTTIE_TEMP": {
          const tempC = (raw[0] << 24) >> 24;
          parsed = {temperature: tempC};
          break;
        } 
        
        case "CAN_REARTIE_TEMP": {
          const tempC = (raw[0] << 24) >> 24;
          parsed = {temperature: tempC};
          break;
        }
        
        case "CAN_ACCEL": {
        parsed = {
            accel2_x: signed(raw[0] + raw[1] * Math.pow(2, 8), 16) / 128, 
            accel2_y: signed(raw[2] + raw[3] * Math.pow(2, 8), 16) / 128, 
            accel2_z: signed(raw[4] + raw[5] * Math.pow(2, 8), 16) / 128 
          };
          break;
        }
        
        
        
    
        
        default: {
        parsed = null;
        break;
        }
      }
      break;
    }

    case 'ADC': {
      const resolution = 12; // ADC resolution in bits
      const max = (1 << resolution) - 1;

      switch (key) {
        case "ADC_INIT":
        case "ADC_CPU": {
          parsed = {
            CPU_TEMP: (raw[0] + raw[1] * Math.pow(2, 8)) / 10,
            INPUT_VOLTAGE: (raw[2] + raw[3] * Math.pow(2, 8)) / max * 3.3 * 8,
          };
          break;
        }
        case 'ADC_DIST': {
          function calc_position(value) {
            const adc_volatage = 3.3;
            const adc_resolution = 12;
            const adc_max_count = (1 << 12) - 1;
            const sensor_max_travel = 100; // mm
            const sensor_voltage = 5;

            const voltage = value / adc_max_count * adc_volatage;
            // const dist = voltage / sensor_voltage * sensor_max_travel;

            return voltage.toFixed(1);
          }
          parsed = {
            DIST_RL: calc_position(raw[0] + raw[1] * Math.pow(2, 8)),
            DIST_FL: calc_position(raw[2] + raw[3] * Math.pow(2, 8)),
            DIST_FR: calc_position(raw[4] + raw[5] * Math.pow(2, 8)),
            DIST_RR: calc_position(raw[6] + raw[7] * Math.pow(2, 8)),
          };
          break;
        }

        case 'ADC_A3': {
          function adc_to_voltage(value) {
            const adc_volatage = 3.3;
            const adc_resolution = 12;
            const adc_max_count = (1 << 12) - 1;
  
            const voltage = value / adc_max_count * adc_volatage;
                
            return voltage.toFixed(3);
          }
          parsed = {
            ADC_A3_FL: adc_to_voltage(raw[0] + raw[1] * Math.pow(2, 8)),
            ADC_A3_RL: adc_to_voltage(raw[2] + raw[3] * Math.pow(2, 8)),
            ADC_A3_FR: adc_to_voltage(raw[4] + raw[5] * Math.pow(2, 8)),
            ADC_A3_RR: adc_to_voltage(raw[6] + raw[7] * Math.pow(2, 8)),
          };
          break;
        }

        default:
          parsed = null;
          break;
      }
      break;
    } // case 'ADC'
    case 'TIM':
      break;

    case 'LCD': 
      break;
    case 'GPS': {
      switch (key) {
        case "GPS_INIT":
            break;
        case 'GPS_POS': {
          const raw_lat = (raw[0] + raw[1] * Math.pow(2, 8) + raw[2] * Math.pow(2, 16) + raw[3] * Math.pow(2, 24)) * 0.0000001;
          const raw_lon = (raw[4] + raw[5] * Math.pow(2, 8) + raw[6] * Math.pow(2, 16) + raw[7] * Math.pow(2, 24)) * 0.0000001;

          parsed = {
            lat: Math.floor(raw_lat) + (((raw_lat % 1) * 100).toFixed(5) / 60),
            lon: Math.floor(raw_lon) + (((raw_lon % 1) * 100).toFixed(5) / 60),
          };
          break;
        }

        case 'GPS_VEC': {
          parsed = {
            speed: (raw[0] + raw[1] * Math.pow(2, 8) + raw[2] * Math.pow(2, 16) + raw[3] * Math.pow(2, 24)) * 0.01 * 1.852,
            course: (raw[4] + raw[5] * Math.pow(2, 8) + raw[6] * Math.pow(2, 16) + raw[7] * Math.pow(2, 24))
          };
          break;
        }

        case 'GPS_TIME': {
          parsed = {
            utc_date: (raw[0] + raw[1] * Math.pow(2, 8) + raw[2] * Math.pow(2, 16) + raw[3] * Math.pow(2, 24)),
            utc_time: (raw[4] + raw[5] * Math.pow(2, 8) + raw[6] * Math.pow(2, 16) + raw[7] * Math.pow(2, 24))
          };
          break;
        }
        

        default:
          parsed = null;
          break;
      }
      break;
    } // case 'GPS'
  } // switch (source)

  return parsed;
}

function signed(value, bit) {
  return value > Math.pow(2, bit - 1) - 1 ? value - Math.pow(2, bit) : value;
}

// Node.js 환경인지 확인 후 exports 사용
if (typeof exports !== "undefined") {
  exports.LOG_LEVEL = LOG_LEVEL;
  exports.LOG_SOURCE = LOG_SOURCE;
  exports.LOG_KEY = LOG_KEY;
  exports.translate = translate;
}