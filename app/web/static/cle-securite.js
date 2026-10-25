// Clés de sécurité FIDO2 / WebAuthn (YubiKey en USB-C ou NFC) : inscription et connexion.
// Fichier séparé : la CSP interdit tout script en ligne. Les données binaires voyagent en
// base64url, comme le serveur (bibliothèque webauthn) les attend.
"use strict";

const base64url = {
  decode(text) {
    const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
  },
  encode(buffer) {
    const bytes = new Uint8Array(buffer);
    let text = "";
    for (const byte of bytes) text += String.fromCharCode(byte);
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

function creationOptions(options) {
  options.challenge = base64url.decode(options.challenge);
  options.user.id = base64url.decode(options.user.id);
  for (const credential of options.excludeCredentials || []) {
    credential.id = base64url.decode(credential.id);
  }
  return { publicKey: options };
}

function requestOptions(options) {
  options.challenge = base64url.decode(options.challenge);
  for (const credential of options.allowCredentials || []) {
    credential.id = base64url.decode(credential.id);
  }
  return { publicKey: options };
}

function toJSON(credential) {
  const response = credential.response;
  const out = {
    id: credential.id,
    rawId: base64url.encode(credential.rawId),
    type: credential.type,
    response: { clientDataJSON: base64url.encode(response.clientDataJSON) },
  };
  if (response.attestationObject) {
    out.response.attestationObject = base64url.encode(response.attestationObject);
    if (response.getTransports) out.response.transports = response.getTransports();
  }
  if (response.authenticatorData) {
    out.response.authenticatorData = base64url.encode(response.authenticatorData);
    out.response.signature = base64url.encode(response.signature);
    if (response.userHandle) out.response.userHandle = base64url.encode(response.userHandle);
  }
  return out;
}

async function post(url, body, csrf) {
  const headers = { "Content-Type": "application/json" };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const reply = await fetch(url, { method: "POST", headers, body: JSON.stringify(body), credentials: "same-origin" });
  const data = await reply.json().catch(() => ({}));
  if (!reply.ok) throw new Error(data.erreur || data.detail || "Échec de l'opération.");
  return data;
}

function show(element, text, isError) {
  if (!element) return;
  element.textContent = text;
  element.className = isError ? "negatif" : "discret";
  element.hidden = false;
}

const LOGIN_REFUSED = "Connexion refusée : mot de passe incorrect, clé non reconnue ou opération annulée. Vérifiez le mot de passe, puis touchez la clé ; sans clé enregistrée, utilisez le code à 6 chiffres.";

function explain(error) {
  if (error && error.name === "NotAllowedError") return "Opération annulée ou délai dépassé : réessayer en touchant la clé.";
  if (error && error.name === "InvalidStateError") return "Cette clé est déjà enregistrée.";
  return (error && error.message) || "Échec de l'opération.";
}

document.addEventListener("DOMContentLoaded", () => {
  const supported = !!(window.PublicKeyCredential && navigator.credentials);

  const loginButton = document.getElementById("connexion-cle");
  if (loginButton) {
    const status = document.getElementById("statut-cle");
    if (!supported) {
      loginButton.disabled = true;
      show(status, "Ce navigateur ne gère pas les clés de sécurité.", true);
    }
    loginButton.addEventListener("click", async () => {
      const form = loginButton.form;
      const username = form.elements.identifiant.value;
      const password = form.elements.mot_de_passe.value;
      if (!username || !password) {
        show(status, "Identifiant et mot de passe d'abord, puis la clé.", true);
        return;
      }
      try {
        show(status, "Touchez votre clé (ou approchez-la du téléphone)…", false);
        const options = await post("/connexion/cle/options", { identifiant: username, mot_de_passe: password });
        const credential = await navigator.credentials.get(requestOptions(options));
        const result = await post("/connexion/cle", { credential: toJSON(credential), suivant: form.elements.suivant.value });
        window.location.assign(result.redirection);
      } catch (error) {
        // La réponse du serveur ne dit pas si le mot de passe était bon (pas d'indice sans clé).
        const refused = error && (error.name === "NotAllowedError" || error.message === "Identifiants incorrects.");
        show(status, refused ? LOGIN_REFUSED : explain(error), true);
      }
    });
  }

  const registerButton = document.getElementById("ajouter-cle");
  if (registerButton) {
    const status = document.getElementById("statut-inscription");
    if (!supported) {
      registerButton.disabled = true;
      show(status, "Ce navigateur ne gère pas les clés de sécurité.", true);
    }
    registerButton.addEventListener("click", async () => {
      const form = registerButton.form;
      const csrf = form.elements.csrf.value;
      try {
        show(status, "Touchez votre clé (ou approchez-la du téléphone)…", false);
        const options = await post("/securite/cles/options", { mot_de_passe: form.elements.mot_de_passe.value }, csrf);
        const credential = await navigator.credentials.create(creationOptions(options));
        const result = await post("/securite/cles", { credential: toJSON(credential), nom: form.elements.nom.value }, csrf);
        window.location.assign(result.redirection);
      } catch (error) {
        show(status, explain(error), true);
      }
    });
  }
});
