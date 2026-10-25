// Relais de l'extension MCP : node --test tests/extension/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createRelay } = require("../../app/assistant/extension/server/index.js");

const ENV = { FINANCES_URL: "https://pi.exemple.ts.net/mcp", FINANCES_TOKEN: "fia_jeton-factice" };

function fakeServer(reply) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options, body: JSON.parse(options.body) });
    return reply(JSON.parse(options.body));
  };
  return { calls, fetchImpl };
}

function response(status, body) {
  return { status, ok: status >= 200 && status < 300, text: async () => (body === undefined ? "" : JSON.stringify(body)) };
}

function relayWith(reply, env = ENV) {
  const out = [];
  const server = fakeServer(reply);
  const relay = createRelay({ env, write: (text) => out.push(JSON.parse(text)), fetchImpl: server.fetchImpl });
  return { relay, out, calls: server.calls };
}

test("transmet la requête avec le jeton et renvoie la réponse", async () => {
  const { relay, out, calls } = relayWith((m) => response(200, { jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2025-11-25" } }));
  await relay.forward(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }));
  assert.equal(calls[0].url, ENV.FINANCES_URL);
  assert.equal(calls[0].options.headers.Authorization, "Bearer fia_jeton-factice");
  assert.equal(calls[0].options.redirect, "error");
  assert.deepEqual(out, [{ jsonrpc: "2.0", id: 1, result: { protocolVersion: "2025-11-25" } }]);
  await relay.forward(JSON.stringify({ jsonrpc: "2.0", id: 2, method: "ping" }));
  assert.equal(calls[1].options.headers["MCP-Protocol-Version"], "2025-11-25");
});

test("une notification acceptée (202) ne produit aucune sortie", async () => {
  const { relay, out } = relayWith(() => response(202));
  await relay.forward(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }));
  assert.deepEqual(out, []);
});

test("les refus et pannes deviennent des erreurs lisibles, avec l'identifiant de la requête", async () => {
  const refused = relayWith(() => response(401, { jsonrpc: "2.0", id: null, error: { code: -32600, message: "Jeton révoqué." } }));
  await refused.relay.forward(JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/list" }));
  assert.equal(refused.out[0].id, 7);
  assert.match(refused.out[0].error.message, /HTTP 401.*Jeton révoqué/);

  const down = relayWith(() => { throw new Error("ECONNREFUSED"); });
  await down.relay.forward(JSON.stringify({ jsonrpc: "2.0", id: 8, method: "ping" }));
  assert.match(down.out[0].error.message, /Tailscale/);

  const garbled = relayWith(() => ({ status: 502, ok: false, text: async () => "<html>" }));
  await garbled.relay.forward(JSON.stringify({ jsonrpc: "2.0", id: 9, method: "ping" }));
  assert.match(garbled.out[0].error.message, /illisible/);
});

test("JSON invalide et configuration manquante", async () => {
  const { relay, out } = relayWith(() => response(200, {}));
  await relay.forward("{pas du json");
  assert.equal(out[0].error.code, -32700);
  const unconfigured = relayWith(() => response(200, {}), { FINANCES_URL: "http://pi/mcp", FINANCES_TOKEN: "x" });
  await unconfigured.relay.forward(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }));
  assert.match(unconfigured.out[0].error.message, /HTTPS/);
  assert.equal(unconfigured.calls.length, 0);
});
