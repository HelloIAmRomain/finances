#!/usr/bin/env node
// Relais MCP local du client MCP du PC vers l'application Finances perso (docs/spec-mcp.md §3).
// Lit les messages JSON-RPC sur l'entrée standard (un par ligne), les poste en HTTPS sur le
// serveur /mcp du Raspberry Pi (Tailscale) avec le jeton, et écrit les réponses sur la sortie
// standard. Aucune dépendance : fetch et readline de Node suffisent. Le jeton n'est jamais écrit
// dans les journaux.
"use strict";

const readline = require("node:readline");

const TIMEOUT_MS = 60_000;

function log(message) {
  process.stderr.write(`[finances] ${message}\n`);
}

function configuration(env) {
  const address = (env.FINANCES_URL || "").trim();
  const token = (env.FINANCES_TOKEN || "").trim();
  if (!address.startsWith("https://")) {
    return { problem: "Adresse du serveur absente ou non HTTPS : la corriger dans les réglages de l'extension." };
  }
  if (!token.startsWith("fia_")) {
    return { problem: "Jeton absent ou invalide : en créer un sur la page Sécurité de l'application." };
  }
  return { address, token };
}

function createRelay({ env = process.env, write = (text) => process.stdout.write(text), fetchImpl = fetch } = {}) {
  const config = configuration(env);
  let protocolVersion = null;

  function send(message) {
    write(`${JSON.stringify(message)}\n`);
  }

  function fail(id, message) {
    if (id === undefined || id === null) {
      log(message);
      return;
    }
    send({ jsonrpc: "2.0", id, error: { code: -32000, message } });
  }

  async function forward(line) {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "JSON illisible." } });
      return;
    }
    const id = message && message.id;
    if (config.problem) {
      fail(id, config.problem);
      return;
    }
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${config.token}`,
    };
    if (protocolVersion) headers["MCP-Protocol-Version"] = protocolVersion;
    let response;
    try {
      response = await fetchImpl(config.address, {
        method: "POST",
        headers,
        body: JSON.stringify(message),
        redirect: "error", // le jeton ne suit jamais une redirection, quelle que soit la version de Node
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch (error) {
      fail(id, `Serveur injoignable (${config.address}) : Tailscale est-il connecté ? ${error.message}`);
      return;
    }
    if (response.status === 202) return;
    const text = await response.text();
    let answer;
    try {
      answer = JSON.parse(text);
    } catch {
      fail(id, `Réponse illisible du serveur (HTTP ${response.status}).`);
      return;
    }
    if (!response.ok) {
      const reason = answer && answer.error && answer.error.message;
      fail(id, `Refusé par le serveur (HTTP ${response.status}) : ${reason || "erreur inconnue"}`);
      return;
    }
    if (message.method === "initialize" && answer.result && answer.result.protocolVersion) {
      protocolVersion = answer.result.protocolVersion;
    }
    send(answer);
  }

  return { forward, config };
}

function main() {
  const relay = createRelay();
  if (relay.config.problem) log(relay.config.problem);
  const pending = new Set();
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on("line", (line) => {
    const task = relay.forward(line).catch((error) => log(`erreur inattendue : ${error.message}`));
    pending.add(task);
    task.finally(() => pending.delete(task));
  });
  input.on("close", async () => {
    await Promise.allSettled([...pending]);
    process.exit(0);
  });
}

if (require.main === module) main();

module.exports = { createRelay, configuration };
