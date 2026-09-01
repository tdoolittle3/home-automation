#!/usr/bin/env node
/*
 * Provision the ladybird monitor set in Uptime Kuma.
 *
 * Credentials come from the environment, never from this file:
 *   KUMA_USER  (default: admin)
 *   KUMA_PW    (required)
 *   KUMA_URL   (default: http://127.0.0.1:3001)
 *
 * Prints a plan and exits. Pass --apply to actually create anything.
 * Idempotent: monitors are matched by name, existing ones are left alone.
 */
const io = require("socket.io-client");
const crypto = require("crypto");

const URL = process.env.KUMA_URL || "http://127.0.0.1:3001";
const USER = process.env.KUMA_USER || "admin";
const PW = process.env.KUMA_PW;
const APPLY = process.argv.includes("--apply");
const HOST = process.env.LADYBIRD_HOST || "192.168.0.13";

if (!PW) { console.error("KUMA_PW is not set. Refusing to continue."); process.exit(1); }

const push = () => crypto.randomBytes(16).toString("hex");
const PUSH_TOKENS = { "frigate-recording": push(), "storage-guard": push() };

// Sensible defaults: 60s beat, 3 retries at 60s => ~3 min before it pages,
// so a two-second blip stays quiet.
const base = {
  interval: 60, retryInterval: 60, resendInterval: 0, maxretries: 3,
  timeout: 20, accepted_statuscodes: ["200-299"], active: true,
  method: "GET", maxredirects: 10, ignoreTls: false, upsideDown: false,
  expiryNotification: false, description: null,
};

const STATS = `http://${HOST}:5000/api/stats`;

// parent is a NAME here; resolved to an id before sending.
const MONITORS = [
  { name: "frigate-api", type: "keyword", url: `http://${HOST}:5000/api/version`,
    keyword: "0.17", invertKeyword: false,
    description: "Frigate API answering. Parent of every frigate-* check." },

  { name: "frigate-driveway-stream", type: "json-query", url: STATS, parent: "frigate-api",
    jsonPath: "cameras.driveway.camera_fps > 0", expectedValue: "true",
    description: "RTSP pull alive for driveway." },
  { name: "frigate-backyard-stream", type: "json-query", url: STATS, parent: "frigate-api",
    jsonPath: "cameras.backyard.camera_fps > 0", expectedValue: "true",
    description: "RTSP pull alive for backyard." },

  // These two would have caught detect.enabled defaulting to false in 0.17.
  { name: "frigate-driveway-detect", type: "json-query", url: STATS, parent: "frigate-api",
    jsonPath: "cameras.driveway.detection_enabled", expectedValue: "true",
    description: "Object detection actually running on driveway." },
  { name: "frigate-backyard-detect", type: "json-query", url: STATS, parent: "frigate-api",
    jsonPath: "cameras.backyard.detection_enabled", expectedValue: "true",
    description: "Object detection actually running on backyard." },

  { name: "frigate-ui", type: "port", hostname: HOST, port: 8971, parent: "frigate-api",
    description: "Authenticated Frigate UI." },

  // Fed by frigate-guard.sh, which only beats when streams, detection AND
  // recording-to-disk are all healthy. Silence is the alert.
  { name: "frigate-recording", type: "push", parent: "frigate-api",
    interval: 300, maxretries: 1, pushToken: PUSH_TOKENS["frigate-recording"],
    description: "Recording segments hitting disk. Beat by frigate-guard.timer (2 min)." },

  // Cameras are NOT children of frigate-api: a camera can die while Frigate is fine.
  { name: "cam-driveway", type: "ping", hostname: "10.10.10.201", maxretries: 4,
    description: "Camera 1 on the isolated island." },
  { name: "cam-backyard", type: "ping", hostname: "10.10.10.202", maxretries: 4,
    description: "Camera 2 on the isolated island." },

  { name: "home-assistant", type: "http", url: `http://${HOST}:8123`,
    accepted_statuscodes: ["200-299", "300-399"] },
  { name: "mosquitto", type: "port", hostname: HOST, port: 1883 },
  { name: "jellyfin", type: "http", url: `http://${HOST}:8096`,
    accepted_statuscodes: ["200-299", "300-399"] },

  { name: "storage-guard", type: "push", interval: 900, maxretries: 1,
    pushToken: PUSH_TOKENS["storage-guard"],
    description: "Beat by disk-guard.timer (10 min) only while storage is healthy." },

  { name: "wan", type: "ping", hostname: "1.1.1.1", maxretries: 5,
    description: "Internet reachability. Independent of everything else." },
];

const socket = io(URL, { transports: ["websocket"], reconnection: false });
const emit = (ev, ...a) => new Promise((res, rej) => {
  const t = setTimeout(() => rej(new Error(`timeout on ${ev}`)), 20000);
  socket.emit(ev, ...a, (r) => { clearTimeout(t); r && r.ok === false ? rej(new Error(r.msg || ev)) : res(r); });
});

let monitorList = {}, notificationList = [];
socket.on("monitorList", (l) => { monitorList = l || {}; });
socket.on("notificationList", (l) => { notificationList = l || []; });

socket.on("connect_error", (e) => { console.error("connect failed:", e.message); process.exit(1); });

socket.on("connect", async () => {
  try {
    await emit("login", { username: USER, password: PW, token: "" });
    console.log(`Logged in to ${URL} as ${USER}\n`);
    await new Promise((r) => setTimeout(r, 1500)); // let pushed lists arrive

    const existing = new Set(Object.values(monitorList).map((m) => m.name));
    const notifIDs = {};
    notificationList.forEach((n) => { notifIDs[n.id] = true; });

    if (notificationList.length === 0) {
      console.log("!! NO NOTIFICATION CHANNELS EXIST.");
      console.log("!! Monitors will be created but CANNOT alert anyone.");
      console.log("!! Create one in Settings > Notifications, then re-run to attach it.\n");
    } else {
      console.log(`Attaching ${notificationList.length} notification channel(s): ` +
        notificationList.map((n) => n.name).join(", ") + "\n");
    }

    const ids = {};
    Object.values(monitorList).forEach((m) => { ids[m.name] = m.id; });

    for (const spec of MONITORS) {
      if (existing.has(spec.name)) {
        console.log(`  skip   ${spec.name} (already exists)`);
        continue;
      }
      const { parent, ...rest } = spec;
      const mon = { ...base, ...rest, notificationIDList: notifIDs,
                    parent: parent ? ids[parent] ?? null : null };

      if (!APPLY) {
        console.log(`  PLAN   ${spec.name}  [${spec.type}]` + (parent ? ` -> child of ${parent}` : ""));
        continue;
      }
      const r = await emit("add", mon);
      ids[spec.name] = r.monitorID;
      console.log(`  create ${spec.name}  [${spec.type}] id=${r.monitorID}` +
                  (parent ? ` -> child of ${parent}` : ""));
    }

    if (APPLY) {
      console.log("\nPush URLs - write these to the server, then chmod 600:");
      for (const [n, t] of Object.entries(PUSH_TOKENS)) {
        const f = n === "storage-guard" ? "kuma-push-url.txt" : "kuma-frigate-push-url.txt";
        console.log(`  /opt/stacks/net/${f}\n    ${URL}/api/push/${t}?`);
      }
    } else {
      console.log("\nPlan only. Re-run with --apply to create these.");
    }
    socket.close(); process.exit(0);
  } catch (e) {
    console.error("FAILED:", e.message); socket.close(); process.exit(1);
  }
});
