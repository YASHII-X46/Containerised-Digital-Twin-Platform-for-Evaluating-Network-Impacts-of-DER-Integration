/* OpenFMB command/event bus client for the UI orchestrator (NATS).
 *
 * Publishes commands to the engines and awaits their events (matched by
 * correlationId) over NATS, so the control panel drives the DT over the
 * OpenFMB message bus. `nats` is required lazily so the rest of the server
 * still loads if the dependency isn't installed yet.
 *
 * Topics use '/' (e.g. openfmb/command/load-engine/generate); NATS subjects use
 * '.', so we translate on the way out/in.
 */
"use strict";
const { randomUUID } = require("crypto");

const NATS_URL = process.env.NATS_URL || `nats://${process.env.BROKER_HOST || "localhost"}:4222`;
const PREFIX = process.env.BUS_PREFIX || "openfmb";

const toSubject = (topic) => topic.replace(/\//g, ".");

class BusClient {
  constructor(url, prefix) {
    this.url = url;
    this.prefix = prefix;
    this.pending = new Map();
    this.subscribed = new Set();
    this.nc = null;
    this.sc = null;
    this.ready = this._connect();
  }

  async _connect() {
    const { connect, StringCodec } = require("nats"); // lazy
    this.nc = await connect({ servers: this.url, name: "ui-bus" });
    this.sc = StringCodec();
  }

  async _ensureSub(subject) {
    if (this.subscribed.has(subject)) return;
    this.subscribed.add(subject);
    const sub = this.nc.subscribe(subject);
    (async () => {
      for await (const m of sub) {
        let msg;
        try { msg = JSON.parse(this.sc.decode(m.data)); } catch { continue; }
        const p = this.pending.get(msg.correlationId);
        if (p) { this.pending.delete(msg.correlationId); clearTimeout(p.timer); p.resolve(msg); }
      }
    })();
  }

  async request(service, action, payload, timeoutMs = 600000) {
    await this.ready;
    await this._ensureSub(toSubject(`${this.prefix}/event/${service}/${action}`));
    const correlationId = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(correlationId);
        reject(new Error(`Bus timeout waiting for ${service}/${action}`));
      }, timeoutMs);
      this.pending.set(correlationId, { resolve, timer });
      const cmd = JSON.stringify({
        messageId: randomUUID(), correlationId, timestamp: new Date().toISOString(), payload,
      });
      this.nc.publish(toSubject(`${this.prefix}/command/${service}/${action}`), this.sc.encode(cmd));
    });
  }
}

let _bus = null;

/** Lazily create the singleton NATS bus client. */
function getBus() {
  if (!_bus) _bus = new BusClient(NATS_URL, PREFIX);
  return _bus;
}

module.exports = { BusClient, getBus };
