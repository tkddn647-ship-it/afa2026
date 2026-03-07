import fs from 'fs';
import express from 'express';
import { Server } from 'socket.io';
import path from 'path';
import winston from 'winston';
import winstonDaily from 'winston-daily-rotate-file';
import { LOG_LEVEL, LOG_SOURCE, LOG_KEY, translate } from '../web/assets/types.js';
import logfilesRouter from './routes/logfiles.mjs';
import { fileURLToPath } from 'url';  // ES 모듈?�서 __dirname ?�용???�해 ?�요
import multer from 'multer';
import recordRouter from './routes/record.mjs';  
import ExcelJS from 'exceljs'; // 추�?


/*****************************************************************************
 * server configurations
 ****************************************************************************/

const app = express();
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.resolve();
const recordings = new Map();

const MIN_RECORDING_INTERVAL_SEC = 0.005; // 200 Hz
let separateLogTransport = null;

function formatRecordingTimestamp(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  const ss = String(date.getSeconds()).padStart(2, '0');
  const ms = String(date.getMilliseconds()).padStart(3, '0');
  return y + '-' + m + '-' + d + ' ' + hh + ':' + mm + ':' + ss + '.' + ms;
}

function buildRecordingSnapshot(ecu) {
  return {
    car: {
      speed: ecu.car.speed,
      accel2: {
        accel2_x: ecu.car.accel2.accel2_x,
        accel2_y: ecu.car.accel2.accel2_y,
        accel2_z: ecu.car.accel2.accel2_z,
      },
      temp: {
        front_tie: ecu.car.temp.front_tie,
        rear_tie: ecu.car.temp.rear_tie,
      },
      accel: ecu.car.accel,
      brake: ecu.car.brake,
      steering: {
        speed: ecu.car.steering.speed,
        angle: ecu.car.steering.angle,
      },
      linear: {
        front_left: ecu.car.linear.front_left,
        front_right: ecu.car.linear.front_right,
        rear_left: ecu.car.linear.rear_left,
        rear_right: ecu.car.linear.rear_right,
      },
    },
    inverter: {
      temperature: {
        igbt: {
          max: { temperature: ecu.inverter.temperature.igbt.max.temperature }
        },
        rtd: {
          rtd1: ecu.inverter.temperature.rtd.rtd1,
          rtd2: ecu.inverter.temperature.rtd.rtd2,
        },
        gatedriver: ecu.inverter.temperature.gatedriver,
        controlboard: ecu.inverter.temperature.controlboard,
        coolant: ecu.inverter.temperature.coolant,
        hotspot: ecu.inverter.temperature.hotspot,
        motor: ecu.inverter.temperature.motor,
      },
      motor: {
        speed: ecu.inverter.motor.speed,
        angle: ecu.inverter.motor.angle,
      },
      current: {
        dc_bus: ecu.inverter.current.dc_bus,
        A: ecu.inverter.current.A,
        B: ecu.inverter.current.B,
        C: ecu.inverter.current.C,
      },
      voltage: {
        dc_bus: ecu.inverter.voltage.dc_bus,
        output: ecu.inverter.voltage.output,
      },
      torque: {
        feedback: ecu.inverter.torque.feedback,
        commanded: ecu.inverter.torque.commanded,
      },
      feedback: {
        id: ecu.inverter.feedback.id,
        iq: ecu.inverter.feedback.iq,
      }
    },
    bms: {
      charge: ecu.bms.charge,
      capacity: ecu.bms.capacity,
      voltage: ecu.bms.voltage,
      current: ecu.bms.current,
      ccl: ecu.bms.ccl,
      dcl: ecu.bms.dcl,
      temperature: {
        max: {
          value: ecu.bms.temperature.max.value,
          id: ecu.bms.temperature.max.id,
        },
        min: {
          value: ecu.bms.temperature.min.value,
          id: ecu.bms.temperature.min.id,
        },
        internal: ecu.bms.temperature.internal,
      },
    },
  };
}

app.use('/api', logfilesRouter);
app.use(express.static(path.join(__dirname, 'public'))); // ?�론?�엔???�일 경로
app.use('/api/record', recordRouter);
app.use('/recorded', express.static(path.join(__dirname, 'recorded')));

const config = JSON.parse(fs.readFileSync('config.json'));

const server = app.listen(config.port, () => {
  logger.info('SERVER STARTUP', { port: config.port });
});

// Create /recorded directory if it doesn't exist
const recordedPath = path.join(__dirname, 'recorded');
if (!fs.existsSync(recordedPath)) {
  fs.mkdirSync(recordedPath);
}


/*****************************************************************************
 * logger configurations
 ****************************************************************************/
const logger = winston.createLogger({
  transports: [
    new winstonDaily({
      level: 'info',
      datePattern: 'YYYY-MM-DD',
      dirname: './log',
      filename: `%DATE%.log`,
    })
  ],
  format: winston.format.combine(
    winston.format.timestamp({
      format: 'YYYY-MM-DD HH:mm:ss.SSS'
    }),
    winston.format.json()
  ),
  exitOnError: false,
});

// 경로 ?�보 로깅
logger.info('Server started', {
  dirname: __dirname,
  typesJsPath: path.join(__dirname, '../web/assets/types.js'),
  publicPath: path.join(__dirname, 'public')
});

/*****************************************************************************
 * socket server configurations
 ****************************************************************************/
const io = new Server(server, {
  pingInterval: 3000,
  pingTimeout: 10000
});

/*****************************************************************************
 * socket event handlers
 ****************************************************************************/
io.sockets.on('connection', socket => {
  // verify channel
  console.log('Types.js 경로:', path.join(__dirname, '../web/assets/types.js'));
  console.log('Public ?�렉?�리 경로:', path.join(__dirname, 'public'));

  const channel = config.channels.find(x => x.name === socket.handshake.query.channel);
  if (!channel || (channel.key !== socket.handshake.query.key)) {
    socket.disconnect();
    logger.error('UNAUTHORIZED DEVICE', {
      id: socket.id,
      ip: socket.handshake.headers['x-forwarded-for'] || socket.handshake.address,
      channel: socket.handshake.query.channel,
      key: socket.handshake.query.key
    });
    return;
  }
  socket.on("start_log_recording", data => {
    const filename = data.filename;
    if (!separateLogTransport) {
      separateLogTransport = new winston.transports.File({
        level: 'info',
        dirname: './separated_logs',  // 별도 로그 ?�???�더 (미리 ?�성 ?�요)
        filename: `${filename}.log`,
        options: { flags: 'a' }
      });
      logger.add(separateLogTransport);
      logger.info("?�?�용 로그 기록 ?�작", { filename: filename });
    } else {
      // ?��? 로그 기록 중인 경우, ???�청?� 무시?�거???�림 처리
      //logger.info("?��? 별도 로그 기록 중입?�다.", { filename: filename });
    }
    
  });  
    
    //?�기부??추�???  // 기록 ?�작
  socket.on('start_recording', ({ intervalSec = MIN_RECORDING_INTERVAL_SEC } = {}) => {
    if (recordings.has(socket.id)) return;
    const reqInterval = Number(intervalSec);
    const safeIntervalSec = Number.isFinite(reqInterval)
      ? Math.max(MIN_RECORDING_INTERVAL_SEC, reqInterval)
      : MIN_RECORDING_INTERVAL_SEC;
    const intervalNs = BigInt(Math.round(safeIntervalSec * 1e9));
    const startedAtMs = Date.now();
    let sampleIndex = 0;
    const buffer = [];
    let timer = null;
    let active = true;
    let nextTickNs = process.hrtime.bigint() + intervalNs;
    const startedAtNs = process.hrtime.bigint();
    let droppedTicks = 0;
    const sample = () => {
      if (!active) return;
      const nowNs = process.hrtime.bigint();
      const elapsedNs = BigInt(sampleIndex) * intervalNs;
      const targetMs = startedAtMs + Number(elapsedNs / 1000000n);
      buffer.push({
        timestamp: formatRecordingTimestamp(new Date(targetMs)),
        sample_index: sampleIndex,
        monotonic_ns: nowNs.toString(),
        data: buildRecordingSnapshot(ECU)
      });
      sampleIndex += 1;
      nextTickNs += intervalNs;
      while (nextTickNs <= nowNs) {
        nextTickNs += intervalNs;
        droppedTicks += 1;
      }
      const delayNs = nextTickNs - process.hrtime.bigint();
      const delayMs = delayNs > 0n ? Number(delayNs / 1000000n) : 0;
      timer = setTimeout(sample, delayMs);
    };
    timer = setTimeout(sample, Number(intervalNs / 1000000n));
    recordings.set(socket.id, {
      stop: () => {
        active = false;
        if (timer) clearTimeout(timer);
      },
      buffer,
      intervalSec: safeIntervalSec,
      startedAtNs,
      getDroppedTicks: () => droppedTicks
    });
    socket.emit('recording_started', {
      targetHz: Math.round(1 / safeIntervalSec),
      intervalSec: safeIntervalSec
    });
  });

  // 기록 중�?
  socket.on('stop_recording', async () => {
    const rec = recordings.get(socket.id);
    if (!rec) return socket.emit('error', '기록 중이 ?�닙?�다.');
    rec.stop();
    recordings.delete(socket.id);

    const wb = new ExcelJS.Workbook();
    const ws = wb.addWorksheet('Telemetry');
    ws.columns = [
      //sensor
      { header: 'Timestamp', key: 'timestamp', width: 25 },
      { header: 'Sample_index', key: 'sample_index', width: 12 },
      { header: 'Speed',     key: 'speed',     width: 10 },
      { header: 'Accel X',   key: 'accelX',    width: 10 },
      { header: 'Accel Y',   key: 'accelY',    width: 10 },
      { header: 'Accel Z',   key: 'accelZ',    width: 10 },
      { header: 'front_Tire', key: 'front_Tire', width: 10 },
      { header: 'rear_Tire',  key: 'rear_Tire', width: 10 },
      { header: 'accel_p',   key: 'accel_p',    width: 10 },
      { header: 'break_p',   key: 'break_p',    width: 10 },
      { header: 'steering_speed',   key: 'steering_speed',    width: 10 },
      { header: 'steering_angle',   key: 'steering_angle',    width: 10 },
      { header: 'linear_fl',   key: 'linear_fl',    width: 10 },
      { header: 'linear_fr',   key: 'linear_fr',    width: 10 },
      { header: 'linear_rl',   key: 'linear_rl',    width: 10 },
      { header: 'linear_rr',   key: 'linear_rr',    width: 10 },
      
      //moter
      { header: 'INV_TEMP_IGT', key: 'INV_TEMP_IGT', width: 10 },
      { header: 'INV_TEMP_RTD1',  key: 'INV_TEMP_RTD1', width: 10 },
      { header: 'INV_TEMP_RTD2',   key: 'INV_TEMP_RTD2',    width: 10 },
      { header: 'INV_TEMP_gatedriver',   key: 'INV_TEMP_gatedriver',    width: 10 },
      { header: 'INV_TEMP_controlboard',   key: 'INV_TEMP_controlboard',    width: 10 },       
      { header: 'INV_TEMP_coolant', key: 'INV_TEMP_coolant', width: 10 },
      { header: 'INV_TEMP_hotspot',  key: 'INV_TEMP_hotspot', width: 10 },
      { header: 'INV_TEMP_motor',   key: 'INV_TEMP_motor',    width: 10 },
      { header: 'INV_moter_speed',   key: 'INV_moter_speed',    width: 10 },
      { header: 'INV_moter_angle',   key: 'INV_moter_angle',    width: 10 },        
      { header: 'INV_dc_currnet', key: 'INV_dc_currnet', width: 10 },
      { header: 'INV_A_currnet', key: 'INV_A_currnet', width: 10 },
      { header: 'INV_B_currnet', key: 'INV_B_currnet', width: 10 },
      { header: 'INV_C_currnet', key: 'INV_C_currnet', width: 10 },
      { header: 'INV_voltage',  key: 'INV_voltage', width: 10 },
      { header: 'INV_voltage_output',   key: 'INV_voltage_output',    width: 10 },
      { header: 'INV_torque_feedback',   key: 'INV_torque_feedback',    width: 10 },
      { header: 'INV_torque_commanded',   key: 'INV_torque_commanded',    width: 10 },
      { header: 'INV_id_feedback',   key: 'INV_id_feedback',    width: 10 },
      { header: 'INV_iq_feedback',   key: 'INV_iq_feedback',    width: 10 },      
     
      //bms
      { header: 'BMS_charge', key: 'BMS_charge', width: 10 },
      { header: 'BMS_capacity',  key: 'BMS_capacity', width: 10 },
      { header: 'BMS_voltage',   key: 'BMS_voltage',    width: 10 },
      { header: 'BMS_currnet',   key: 'BMS_current',    width: 10 },
      { header: 'BMS_ccl',   key: 'BMS_ccl',    width: 10 },
      { header: 'BMS_dcl', key: 'BMS_dcl', width: 10 },
      { header: 'BMS_TEMP_maxvalue',  key: 'BMS_TEMP_maxvalue', width: 10 },
      { header: 'BMS_TEMP_maxid',   key: 'BMS_TEMP_maxid',    width: 10 },
      { header: 'BMS_TEMP_minvalue',   key: 'BMS_TEMP_minvalue',    width: 10 },
      { header: 'BMS_TEMP_minid',   key: 'BMS_TEMP_minid',    width: 10 },
      { header: 'BMS_TEMP_internal',  key: 'BMS_TEMP_internal', width: 10 },
      
    ];

    for (const row of rec.buffer) {
      ws.addRow({
      
        //sensor
        timestamp: row.timestamp,
        sample_index: row.sample_index,
        speed:     row.data.car.speed,
        accelX:    row.data.car.accel2.accel2_x,
        accelY:    row.data.car.accel2.accel2_y,
        accelZ:    row.data.car.accel2.accel2_z,
        front_Tire: row.data.car.temp.front_tie,
        rear_Tire:     row.data.car.temp.rear_tie,
        accel_p:    row.data.car.accel,
        break_p:    row.data.car.brake,
        steering_speed:    row.data.car.steering.speed,
        steering_angle:     row.data.car.steering.angle,
        linear_fl:    row.data.car.linear.front_left,
        linear_fr:     row.data.car.linear.front_right,
        linear_rl:    row.data.car.linear.rear_left,
        linear_rr:     row.data.car.linear.rear_right,
         
        
        //moter
        INV_TEMP_IGT:    row.data.inverter.temperature.igbt.max.temperature,
        INV_TEMP_RTD1:    row.data.inverter.temperature.rtd.rtd1,
        INV_TEMP_RTD2:    row.data.inverter.temperature.rtd.rtd2,
        INV_TEMP_gatedriver: row.data.inverter.temperature.gatedriver,
        INV_TEMP_controlboard:     row.data.inverter.temperature.controlboard,
        INV_TEMP_coolant:    row.data.inverter.temperature.coolant,
        INV_TEMP_hotspot:    row.data.inverter.temperature.hotspot,
        INV_TEMP_motor:    row.data.inverter.temperature.motor,
        INV_moter_speed:    row.data.inverter.motor.speed,
        INV_moter_angle:    row.data.inverter.motor.angle,
        INV_dc_currnet:    row.data.inverter.current.dc_bus,
        INV_A_currnet:    row.data.inverter.current.A,
        INV_B_currnet:    row.data.inverter.current.B,
        INV_C_currnet:    row.data.inverter.current.C, 
        INV_voltage:     row.data.inverter.voltage.dc_bus,
        INV_voltage_output:    row.data.inverter.voltage.output,
        INV_torque_feedback:    row.data.inverter.torque.feedback,
        INV_torque_commanded:    row.data.inverter.torque.commanded,
        INV_id_feedback:    row.data.inverter.feedback.id,
        INV_iq_feedback:    row.data.inverter.feedback.iq,



        //bms
        BMS_charge: row.data.bms.charge,
        BMS_capacity:     row.data.bms.capacity,
        BMS_voltage:    row.data.bms.voltage,
        BMS_current:    row.data.bms.current,
        BMS_ccl:    row.data.bms.ccl,
        BMS_dcl: row.data.bms.dcl,
        BMS_TEMP_maxvalue:     row.data.bms.temperature.max.value,
        BMS_TEMP_maxid:    row.data.bms.temperature.max.id,
        BMS_TEMP_minvalue:    row.data.bms.temperature.min.value,
        BMS_TEMP_minid:    row.data.bms.temperature.min.id,
        BMS_TEMP_internal:    row.data.bms.temperature.internal,
        
      });
    }

    const filename = `rec-${socket.id}-${Date.now()}.xlsx`;
    const filepath = path.join(__dirname, 'recorded', filename);
    await wb.xlsx.writeFile(filepath);

    // ?�라?�언?�에 ?�운로드 URL ?�송
    socket.emit('recording_stopped', { file: `/recorded/${filename}` });
  });
    
    //?�기까�? ?�음
    
    
    
  

  // 로그 기록 ?��? ?�벤??처리
  socket.on("stop_log_recording", () => {
    if (separateLogTransport) {
      logger.info("별도 로그 기록 ?��?");
      logger.remove(separateLogTransport);
      if (typeof separateLogTransport.close === 'function') {
        separateLogTransport.close();
      }
      separateLogTransport = null;
    }
  });

  
  // TMA-1 device register
  if (socket.handshake.query.device) {
    register_device(socket);
  }

  // telemetry client register
  else if (socket.handshake.query.client) {
    register_client(socket);
  }

});

/*****************************************************************************
 * socket register functions
 ****************************************************************************/
function normalizeCameraFramePayload(payload) {
  if (!payload) return null;

  let frame = payload;
  let mime = 'image/jpeg';
  let width = null;
  let height = null;

  if (typeof payload === 'object' && !Buffer.isBuffer(payload)) {
    frame = payload.frame ?? payload.data;
    mime = payload.mime || mime;
    width = Number.isFinite(payload.width) ? payload.width : null;
    height = Number.isFinite(payload.height) ? payload.height : null;
  }

  if (Buffer.isBuffer(frame)) {
    frame = frame.toString('base64');
  } else if (typeof frame === 'string') {
    const dataUri = frame.match(/^data:(.+?);base64,(.+)$/);
    if (dataUri) {
      mime = dataUri[1] || mime;
      frame = dataUri[2];
    }
  } else {
    return null;
  }

  if (!frame || typeof frame !== 'string' || frame.length > 8000000) {
    return null;
  }

  return { frame, mime, width, height };
}

function applySensorPatch(status, patch) {
  if (!patch || typeof patch !== 'object') return false;

  if (typeof patch.path === 'string' && Object.prototype.hasOwnProperty.call(patch, 'value')) {
    const keys = patch.path.split('.').filter(Boolean);
    if (!keys.length) return false;

    let target = status;
    for (let i = 0; i < keys.length - 1; i += 1) {
      const key = keys[i];
      if (!target[key] || typeof target[key] !== 'object') {
        target[key] = {};
      }
      target = target[key];
    }
    target[keys[keys.length - 1]] = patch.value;
    return true;
  }

  if (Array.isArray(patch.updates)) {
    let updated = false;
    for (const item of patch.updates) {
      updated = applySensorPatch(status, item) || updated;
    }
    return updated;
  }

  return false;
}
function register_device(socket) {
  socket.join(socket.handshake.query.channel);

  logger.info('DEVICE CONNECTED', {
    id: socket.id,
    ip: socket.handshake.headers['x-forwarded-for'] || socket.handshake.address,
    channel: socket.handshake.query.channel
  });

  

  // emit RTC time fix
  setTimeout(() => socket.emit('rtc_fix', { datetime: new Date().toISOString() }), 1000);

  // Notify all clients about the device connection
  io.emit('device_connected', { message: "device connected" });

  // on SOCKET_DISCONNECTED
  socket.on('disconnect', reason => {
    logger.info('DEVICE DISCONNECTED', {
      id: socket.id,
      ip: socket.handshake.headers['x-forwarded-for'] || socket.handshake.address,
      channel: socket.handshake.query.channel
    });
    ECU.telemetry = false;
    ECU.car.system.ESP = false;
    ECU.camera.online = false;
    
    socket.broadcast.to(socket.handshake.query.channel).emit('socket-lost', { data: reason });
  });

  // on telemetry report
  socket.on('tlog', data => {
    ECU.telemetry = true;
    ECU.car.system.ESP = true;
    process_telemetry(data, socket);
  });

  socket.on('camera_frame', payload => {
    const frame = normalizeCameraFramePayload(payload);
    if (!frame) return;

    ECU.camera.online = true;
    ECU.camera.seq += 1;
    ECU.camera.lastFrameAt = new Date().toISOString();
    ECU.camera.mime = frame.mime;
    ECU.camera.width = frame.width;
    ECU.camera.height = frame.height;
    ECU.camera.lastFrame = frame.frame;

    socket.broadcast.to(socket.handshake.query.channel).emit('camera_frame', {
      frame: frame.frame,
      mime: frame.mime,
      width: frame.width,
      height: frame.height,
      seq: ECU.camera.seq,
      datetime: ECU.camera.lastFrameAt
    });
  });

  socket.on('sensor_update', patch => {
    if (!applySensorPatch(ECU, patch)) return;
    socket.broadcast.to(socket.handshake.query.channel).emit('sensor_update', {
      data: patch,
      status: ECU
    });
  });
}


function register_client(socket) {
  socket.join(socket.handshake.query.channel);

  logger.info('CLIENT CONNECTED', {
    id: socket.id,
    ip: socket.handshake.headers['x-forwarded-for'] || socket.handshake.address,
    channel: socket.handshake.query.channel
  });

  socket.emit('client_connected', {data: null, status: ECU});
  if (ECU.camera.lastFrame) {
    socket.emit('camera_frame', {
      frame: ECU.camera.lastFrame,
      mime: ECU.camera.mime,
      width: ECU.camera.width,
      height: ECU.camera.height,
      seq: ECU.camera.seq,
      datetime: ECU.camera.lastFrameAt
    });
  }
  socket.on('reset-request', () => {
    if (ECU.telemetry) {
      socket.emit('reset-reply', {
        icon: 'error',
        title: '차량 ?�태 초기???�류',
        html: `<code><i class="fa-duotone fa-fw fa-tower-broadcast" style="color: green"></i> ?�격 계측</code> ?�성???�태?�서??차량 ?�태�?초기?�할 ???�습?�다.`,
        showCancelButton: true, showConfirmButton: false, cancelButtonText: '?�인', cancelButtonColor: '#7066e0'
      });
    } else {
      socket.emit('reset-reply', {
        icon: 'warning',
        title: 'Vehicle status reset',
        html: `<code><i class="fa-duotone fa-fw fa-tower-broadcast" style="color: red"></i> ?�격 계측</code> 비활?�화 ?�태?�서 ?�아?�는 ?�전 ?�이?��? 초기?�합?�다.`,
        showCancelButton: true, cancelButtonText: '취소', confirmButtonText: '?�인', confirmButtonColor: '#d33',
        customClass: { confirmButton: 'swal2-two-buttons' }
      });
    }
  });

  socket.on('reset-confirm', () => {
    ECU = JSON.parse(ECU_INIT);
    io.to('client').emit('client_init', { data: null, status: ECU });
  });
}
  
/*****************************************************************************
 * telemetry report handler
 ****************************************************************************/
function process_telemetry(data, socket) {
  // corrupt telemetry data
  if (!('log' in data)) {
    return;
  }

  let raw = data.log;
  data = translate(data.log.match(/.{2}/g).map(x => parseInt(x, 16)));

  if (data instanceof Error) {
    logger.error('PARSE FAILED', {
      id: socket.id,
      ip: socket.handshake.headers['x-forwarded-for'] || socket.handshake.address,
      channel: socket.handshake.query.channel,
      data: data.toString(),
      raw: raw
    });

    return;
  }

  logger.info('REPORT', {
    id: socket.id,
    ip: socket.handshake.headers['x-forwarded-for'] || socket.handshake.address,
    channel: socket.handshake.query.channel,
    data: data,
    raw: raw
  });
  if (data != null) {
    switch (data.source) {
      case "ECU": {
        switch (data.key) {
          case "ECU_STATE": {
            ECU.car.system.HV = data.parsed.HV;
            ECU.car.system.RTD = data.parsed.RTD;
            ECU.car.system.BMS = data.parsed.BMS;
            ECU.car.system.IMD = data.parsed.IMD;
            ECU.car.system.BSPD = data.parsed.BSPD;

            ECU.car.system.ERR = data.parsed.ERR;
            ECU.car.system.SD = data.parsed.SD;
            ECU.car.system.TELEMETRY = data.parsed.TELEMETRY;
            ECU.car.system.CAN = data.parsed.CAN;
            break;
          }
          case "ECU_BOOT":
            break;
          case "ECU_READY":
            break;
          case "SD_INIT":
            break;
          default:
            break;
        }
        break;
      }
      case "ESP": break;
      case "CAN": {
        switch (data.key) {
          case "CAN_INV_TEMP_1": {
            ECU.inverter.temperature.igbt.max = data.parsed.igbt.max;
            ECU.inverter.temperature.gatedriver = data.parsed.gatedriver;
            break;
          }
          case "CAN_INV_TEMP_2": {
            // ECU.inverter.temperature.controlboard = data.parsed.controlboard;
            // ECU.inverter.temperature.rtd.rtd1 = data.parsed.RTD1;
            // ECU.inverter.temperature.rtd.rtd2 = data.parsed.RTD2;
            break;
          }
          case "CAN_INV_TEMP_3": {
            // ECU.inverter.temperature.coolant = data.parsed.coolant;
            ECU.inverter.temperature.hotspot = data.parsed.hotspot;
            ECU.inverter.temperature.motor = data.parsed.motor;
            break;
          }
          case "CAN_INV_ANALOG_IN": {
            ECU.car.accel = data.parsed.AIN1;
            ECU.car.brake = data.parsed.AIN3;
            break;
          }
          case "CAN_INV_MOTOR_POS": {
            ECU.inverter.motor.angle = data.parsed.motor_angle;
            ECU.inverter.motor.speed = data.parsed.motor_speed;
            ECU.car.speed = Math.PI * 0.4572 * 60 * data.parsed.motor_speed / (1000 * 4.9183);
            break;
          }
          case "CAN_INV_CURRENT": {
            ECU.inverter.current.dc_bus = data.parsed.dc_bus_current;
            ECU.inverter.current.A = data.parsed.phaseA;
            ECU.inverter.current.B = data.parsed.phaseB;
            ECU.inverter.current.C = data.parsed.phaseC;

            
            break;
          }
          case "CAN_INV_VOLTAGE": {
            ECU.inverter.voltage.dc_bus = data.parsed.dc_bus_voltage;
            ECU.inverter.voltage.output = data.parsed.output_voltage;
            break;
          }
          case "CAN_INV_STATE": {
            ECU.inverter.state.vsm_state = state.vsm[data.parsed.vsm_state];
            ECU.inverter.state.inverter_state = state.inverter[data.parsed.inverter_state];
            ECU.inverter.state.relay.precharge = (data.parsed.relay_state & (1 << 0)) ? true : false;
            ECU.inverter.state.relay.main = (data.parsed.relay_state & (1 << 1)) ? true : false;
            ECU.inverter.state.relay.pump = (data.parsed.relay_state & (1 << 4)) ? true : false;
            ECU.inverter.state.relay.fan = (data.parsed.relay_state & (1 << 5)) ? true : false;
            ECU.inverter.state.mode = state.inverter_mode[data.parsed.inverter_run_mode];
            ECU.inverter.state.discharge = state.discharge_state[data.parsed.inverter_active_discharge_state];
            ECU.inverter.state.enabled = data.parsed.inverter_enable_state ? true : false;
            ECU.inverter.state.bms_comm = data.parsed.bms_active ? true : false;
            ECU.inverter.state.limit.bms = data.parsed.bms_limiting_torque ? true : false;
            ECU.inverter.state.limit.speed = data.parsed.limit_max_speed ? true : false;
            ECU.inverter.state.limit.hotspot = data.parsed.limit_hot_spot ? true : false;
            ECU.inverter.state.limit.lowspeed = data.parsed.low_speed_limiting ? true : false;
            ECU.inverter.state.limit.coolant = data.parsed.coolant_temperature_limiting ? true : false;
            break;
          }
          case "CAN_INV_FAULT": {
            ECU.inverter.fault.post = [];
            ECU.inverter.fault.run = [];
            for (let i = 0; i < 32; i++) {
              if (data.parsed.POST & (1 << i)) {
                ECU.inverter.fault.post.push(fault.post[i]);
              }
              if (data.parsed.RUN & (1 << i)) {
                ECU.inverter.fault.run.push(fault.run[i + 32]);
              }
            }
            break;
          }
          case "CAN_INV_TORQUE": {
            ECU.inverter.torque.feedback = data.parsed.torque_feedback;
            ECU.inverter.torque.commanded = data.parsed.commanded_torque;
            break;
          }

          case "CAN_BMS_CORE": {
            ECU.bms.charge = data.parsed.soc;
            ECU.bms.capacity = data.parsed.capacity;
            ECU.bms.voltage = data.parsed.voltage;
            ECU.bms.current = data.parsed.current;
            ECU.bms.failsafe = data.parsed.failsafe;
            break;
          }
          case "CAN_BMS_TEMP": {
            ECU.bms.temperature = data.parsed.temperature;
            ECU.bms.dcl = data.parsed.dcl;
            ECU.bms.ccl = data.parsed.ccl;
            break;
          }
          case "CAN_INV_DIGITAL_IN":
              break;
          case "CAN_INV_FLUX": {
            ECU.inverter.feedback.id = data.parsed.Id_feedback;
            ECU.inverter.feedback.iq = data.parsed.Iq_feedback;
          }
              break;
          case "CAN_INV_REF":
              break;
          case "CAN_INV_FLUX_WEAKING":
              break;
          case "CAN_INV_FIRMWARE_VER":
              break;
          case "CAN_INV_DIAGNOSTIC":
              break;
          case "CAN_INV_HIGH_SPD_MSG":
              break;
              
          case "CAN_FRONT_LINEAR_L": {
            ECU.car.linear.front_left = data.parsed.lengthMM;
          break;}
          case "CAN_FRONT_LINEAR_R": {
            ECU.car.linear.front_right = data.parsed.lengthMM;
         break; }
          case "CAN_REAR_LINEAR_L": {
            ECU.car.linear.rear_left = data.parsed.lengthMM;
          break;}
          case "CAN_REAR_LINEAR_R": {
            ECU.car.linear.rear_right = data.parsed.lengthMM;
          break;}
          
          case "CAN_FRONTTIE_TEMP": {
            ECU.car.temp.front_tie = data.parsed.temperature;
          break;}
         
          case "CAN_REARTIE_TEMP": {
            ECU.car.temp.rear_tie = data.parsed.temperature;
          break;}
          
          case "CAN_ACCEL": {
            ECU.car.accel2.accel2_x = data.parsed.accel2_x;
            ECU.car.accel2.accel2_y = data.parsed.accel2_y;
            ECU.car.accel2.accel2_z = data.parsed.accel2_z;
          break;}
          
          
          
          case "CAN_STEERING_WHEEL_ANGLE":{
            ECU.car.steering.angle = data.parsed.angle;
            ECU.car.steering.speed = data.parsed.speed;       
        }
  
        default:
        break;
        }
        break;
      }
      case "ADC": {
        switch (data.key) {
          case "ADC_CPU": {
            ECU.temperature = data.parsed.CPU_TEMP;
            break;
          }
          case "ADC_DIST": {
            ECU.car.position.FL = data.parsed.DIST_FL;
            ECU.car.position.RL = data.parsed.DIST_RL;
            ECU.car.position.FR = data.parsed.DIST_FR;
            ECU.car.position.RR = data.parsed.DIST_RR;
            break;
          }
          case "ADC_INIT":
          default:
            break;
        }
        break;
      }
      case "TIM": break;
      case "ACC": {
        switch (data.key) {
          case "ACC_DATA": {
            ECU.car.acceleration = data.parsed;
            break;
          }
          case "ACC_INIT":
              break;
          default:
            break;
        }
        break;
      }
      case "LCD": break;
      case "GPS": {
        switch (data.key) {
          case "GPS_POS": {
            ECU.car.gps.lat = data.parsed.lat;
            ECU.car.gps.lon = data.parsed.lon;
            break;
          }
          case "GPS_VEC": {
            ECU.car.gps.speed = data.parsed.speed;
            ECU.car.gps.course = data.parsed.course;
            break;
          }
          case "GPS_INIT":
            break;
          case "GPS_TIME":
            break;
          default:
            break;
        }
        break;
      }
    }
  socket.broadcast.to(socket.handshake.query.channel).emit('report', { data: data , status: ECU});
}
}
/*****************************************************************************
 * system state
 ****************************************************************************/
let ECU = {          // initial system status
  telemetry: false,
  session: null,
  temperature: 0,
  camera: {
    online: false,
    seq: 0,
    mime: 'image/jpeg',
    width: null,
    height: null,
    lastFrameAt: null,
    lastFrame: null,
  },
  car: {
    system: {
      HV: false,
      RTD: false,
      BMS: false,
      IMD: false,
      BSPD: false,

      ERR: false,
      SD: false,
      TELEMETRY: false,
      CAN: false,

    },
    position: {
      FL: 0,
      RL: 0,
      FR: 0,
      RR: 0,
    },
    wheel_speed: {
      FL: 0,
      RL: 0,
      FR: 0,
      RR: 0,
    },
    acceleration: {
      x: 0,
      y: 0,
      z: 0,
    },
    gps: {
      lat: 0,
      lon: 0,
      speed: 0,
      course: 0,
    },
    speed: 0,
    accel: 0,
    brake: 0,
    steering: {
      speed: 0,
      angle: 0,
    },
     linear: {
      front_left: 0,
      front_right: 0,
      rear_left: 0,
      rear_right: 0,
    
    },
    
     temp: {
      front_tie: 0,
      rear_tie: 0,
     
     },
     
     accel2: {
      accel2_x: 0,
      accel2_y: 0,
      accel2_z: 0,
     
     },
     airflow: {
      intake_lpm: 0,
      exhaust_lpm: 0,
     },
    
  },
  inverter: {
    temperature: {
      igbt: {
        max: {
          temperature: 0,
          id: "X",
        }
      },
      rtd: {
        rtd1: 0,
        rtd2: 0,
      },
      gatedriver: 0,
      controlboard: 0,
      coolant: 0,
      hotspot: 0,
      motor: 0,
    },
    motor: {
      angle: 0,
      speed: 0,
    },
    current: {
      dc_bus: 0,
      A: 0,
      B: 0,
      C: 0,
    },
    voltage: {
      dc_bus: 0,
      output: 0,
    },
    state: {
      vsm_state: "N/A",
      inverter_state: "N/A",
      relay: {
        precharge: false,
        main: false,
        pump: false,
        fan: false,
      },
      mode: "N/A",
      discharge: "N/A",
      enabled: false,
      bms_comm: false,
      limit: {
        bms: false,
        speed: false,
        hotspot: false,
        lowspeed: false,
        coolant: false
      }
    },
    fault: {
      post: [],
      run: [],
    },
    torque: {
      feedback: 0,
      commanded: 0,
    },
    feedback: {
      id: 0,
      iq: 0,
    },

  },
  bms: {
    charge: 0,
    capacity: 0,
    voltage: 0,
    current: 0,
    ccl: 0,
    dcl: 0,
    failsafe: {
      voltage: false,
      current: false,
      relay: false,
      balancing: false,
      interlock: false,
      thermister: false,
      power: false,
    },
    temperature: {
      max: {
        value: 0,
        id: 0,
      },
      min: {
        value: 0,
        id: 0,
      },
      internal: 0,
    },
  },
}
const ECU_INIT = JSON.stringify(ECU);

const state = { // motor controller properties
  vsm: {
    0: "VSM ?�작",
    1: "Precharge Ready",
    2: "초기충전",
    3: "초기충전 ?�료",
    4: "VSM Wait",
    5: "VSM 준�??�료",
    6: "모터 ?�동",
    7: "FAULT",
    14: "Shutdown",
    15: "Recycle Power"
  },
  inverter: {
    0: "Power On",
    1: "Stop",
    2: "Open Loop", 
    3: "Closed Loop", 
    4: "Wait",
    5: "Internal",
    6: "Internal", 
    7: "Internal",
    8: "Idle Run",
    9: "Idle Stop",
    10: "Internal",
    11: "Internal",
    12: "Internal"
  },
  discharge_state: {
    0: "방전 비활?�화",
    1: "Precharge Ready",
    2: "Discharge Check",
    3: "Discharging",
    4: "방전 ?�료",
  },
  inverter_mode: {
    0: "?�크 모드",
    1: "?�도 모드"
  }
}

const fault = { // motor controller fault properties
  post: {
    0: "Hardware Gate/Desaturation Fault",
    1: "HW Over-current Fault",
    2: "Accelerator Shorted",
    3: "Accelerator Open",
    4: "Current Sensor Low",
    5: "Current Sensor High",
    6: "Module Temperature Low",
    7: "Module Temperature High",
    8: "Control PCB Temperature Low",
    9: "Control PCB Temperature High",
    10: "Gate Drive PCB Temperature Low",
    11: "Gate Drive PCB Temperature High",
    12: "5V Sense Voltage Low",
    13: "5V Sense Voltage High",
    14: "12V Sense Voltage Low",
    15: "12V Sense Voltage High",
    16: "2.5V Sense Voltage Low",
    17: "2.5V Sense Voltage High",
    18: "1.5V Sense Voltage Low",
    19: "1.5V Sense Voltage High",
    20: "DC Bus Voltage High",
    21: "DC Bus Voltage Low",
    22: "Pre-charge Timeout",
    23: "Pre-charge Voltage Failure",
    24: "EEPROM Checksum Invalid",
    25: "EEPROM Data Out of Range",
    26: "EEPROM Update Required",
    27: "Hardware DC Bus Over-Voltage during initialization",
    28: "Reserved",
    29: "Reserved",
    30: "Brake Shorted",
    31: "Brake Open",
  },
  run: {
    32: "Motor Over-speed Fault",
    33: "Over-current Fault",
    34: "Over-voltage Fault",
    35: "Inverter Over-temperature Fault",
    36: "Accelerator Input Shorted Fault",
    37: "Accelerator Input Open Fault",
    38: "Direction Command Fault",
    39: "Inverter Response Time-out Fault",
    40: "Hardware Gate/Desaturation Fault",
    41: "Hardware Over-current Fault",
    42: "Under-voltage Fault",
    43: "CAN Command Message Lost Fault",
    44: "Motor Over-temperature Fault",
    45: "Reserved",
    46: "Reserved",
    47: "Reserved",
    48: "Brake Input Shorted Fault",
    49: "Brake Input Open Fault",
    50: "Module A Over-temperature Fault",
    51: "Module B Over-temperature Fault",
    52: "Module C Over-temperature Fault",
    53: "PCB Over-temperature Fault",
    54: "Gate Drive Board 1 Over-temperature Faul",
    55: "Gate Drive Board 2 Over-temperature Fault",
    56: "Gate Drive Board 3 Over-temperature Fault",
    57: "Current Sensor Fault",
    58: "Reserved",
    59: "Hardware DC Bus Over-Voltage Fault",
    60: "Reserved",
    61: "Reserved",
    62: "Resolver Not Connected",
    63: "Reserved",
  }
}
