// signal.mjs

export class IonSFUJSONRPCSignal {
    constructor(uri) {
      this.socket = new WebSocket(uri);
      this._notifyhandlers = {};
  
      this.socket.addEventListener('open', () => {
        if (this._onopen) this._onopen();
      });
      this.socket.addEventListener('error', (e) => {
        if (this._onerror) this._onerror(e);
      });
      this.socket.addEventListener('close', (e) => {
        if (this._onclose) this._onclose(e);
      });
  
      this.socket.addEventListener('message', async (event) => {
        const resp = JSON.parse(event.data);
        if (resp.method === 'offer') {
          if (this.onnegotiate) this.onnegotiate(resp.params);
        } else if (resp.method === 'trickle') {
          if (this.ontrickle) this.ontrickle(resp.params);
        } else {
          const handler = this._notifyhandlers[resp.method];
          if (handler) handler(resp.params);
        }
      });
    }
  
    on_notify(method, cb) {
      this._notifyhandlers[method] = cb;
    }
  
    async call(method, params) {
      const id = uuid.v4();
      this.socket.send(JSON.stringify({ method, params, id }));
      return new Promise((resolve, reject) => {
        const handler = (event) => {
          const resp = JSON.parse(event.data);
          if (resp.id === id) {
            if (resp.error) reject(resp.error);
            else resolve(resp.result);
            this.socket.removeEventListener('message', handler);
          }
        };
        this.socket.addEventListener('message', handler);
      });
    }
  
    notify(method, params) {
      this.socket.send(JSON.stringify({ method, params }));
    }
  
    async join(sid, uid, offer) {
      return this.call('join', { sid, uid, offer });
    }
  
    trickle(trickle) {
      this.notify('trickle', trickle);
    }
  
    async offer(offer) {
      return this.call('offer', { desc: offer });
    }
  
    answer(answer) {
      this.notify('answer', { desc: answer });
    }
  
    close() {
      this.socket.close();
    }
  
    set onopen(onopen) {
      if (this.socket.readyState === WebSocket.OPEN) {
        onopen();
      }
      this._onopen = onopen;
    }
  
    set onerror(onerror) {
      this._onerror = onerror;
    }
  
    set onclose(onclose) {
      this._onclose = onclose;
    }
  }
  