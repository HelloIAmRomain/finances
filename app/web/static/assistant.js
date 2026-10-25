// Page « Assistant » : envoi sans attendre la réponse, suivi des étapes et du texte en direct,
// dictée (API Web Speech du navigateur), confirmation de suppression.
// Fichier séparé : la CSP interdit tout script en ligne. Le HTML des réponses est produit par le
// serveur (Markdown rendu, HTML brut échappé) : il est inséré tel quel.
"use strict";

const POLL_MS = 700;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function progressBubble() {
  const bubble = element("div", "reponse en-cours");
  bubble.append(element("strong", "", "Assistant"), element("ul", "etapes discret"));
  const text = element("div", "texte");
  text.append(element("p", "discret", "L'assistant réfléchit…"));
  bubble.append(text);
  return bubble;
}

function showError(bubble, message, question, form) {
  bubble.classList.remove("en-cours");
  const text = bubble.querySelector(".texte");
  text.replaceChildren(element("p", "negatif", message));
  if (question && form) {
    const retry = element("button", "secondaire", "Réessayer");
    retry.type = "button";
    retry.addEventListener("click", () => {
      form.elements.texte.value = question;
      form.elements.texte.focus();
    });
    text.append(retry);
  }
}

function follow(bubble, runId, { question, form, conversation, isNew }) {
  const steps = bubble.querySelector(".etapes");
  const text = bubble.querySelector(".texte");
  let since = 0;
  const tick = async () => {
    let reply;
    try {
      reply = await fetch(`/assistant/tours/${encodeURIComponent(runId)}?depuis=${since}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
    } catch {
      window.setTimeout(tick, POLL_MS * 3); // réseau coupé : on réessaie un peu plus tard
      return;
    }
    if (reply.status === 404) {
      showError(bubble, "Réponse introuvable ou expirée : recharger la page.", question, form);
      done(form);
      return;
    }
    const data = await reply.json();
    for (const event of data.evenements) {
      since = Math.max(since, event.n);
      if (event.type === "etape") steps.append(element("li", "", event.texte));
    }
    if (data.html) text.innerHTML = data.html;
    if (data.etat === "en_cours") {
      window.setTimeout(tick, POLL_MS);
      return;
    }
    if (data.etat === "erreur") {
      showError(bubble, data.erreur || "Échec de la réponse.", question, form);
    } else {
      bubble.classList.remove("en-cours");
      if (data.outils.length) {
        const count = data.outils.length;
        text.append(element("p", "discret", `${count} consultation${count > 1 ? "s" : ""} des données`));
      }
      if (isNew) {
        // Nouvelle conversation : recharger pour l'ajouter à la liste, avec son titre.
        // (l'adresse a déjà été mise à jour : un simple changement d'ancre ne rechargerait pas)
        window.history.replaceState(null, "", `/assistant?conversation=${conversation}#fin`);
        window.location.reload();
        return;
      }
    }
    done(form);
  };
  tick();
}

function done(form) {
  if (!form) return;
  form.querySelector("button[type=submit]").disabled = false;
  const status = document.getElementById("statut-dictee");
  if (status) status.hidden = true;
}

function setupSending(form) {
  const thread = document.querySelector(".conversation");
  const end = document.getElementById("fin");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = form.elements.texte;
    const question = field.value.trim();
    if (!question) return;
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    const examples = document.getElementById("exemples");
    if (examples) examples.remove();
    const mine = element("div", "question");
    mine.append(element("strong", "", "Vous"), element("p", "", question));
    const bubble = progressBubble();
    thread.insertBefore(mine, end);
    thread.insertBefore(bubble, end);
    end.scrollIntoView({ block: "end" });
    field.value = "";
    const current = Number(form.elements.conversation.value) || null;
    let reply;
    try {
      reply = await fetch("/assistant/messages", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": form.elements.csrf.value },
        body: JSON.stringify({ texte: question, conversation: current }),
      });
    } catch {
      showError(bubble, "Serveur injoignable : vérifier la connexion.", question, form);
      done(form);
      return;
    }
    const data = await reply.json().catch(() => ({}));
    if (!reply.ok) {
      showError(bubble, data.erreur || "Envoi refusé.", question, form);
      done(form);
      return;
    }
    form.elements.conversation.value = data.conversation;
    if (!current) window.history.replaceState(null, "", `/assistant?conversation=${data.conversation}`);
    follow(bubble, data.tour, { question, form, conversation: data.conversation, isNew: !current });
  });
}

function setupDictation(form) {
  const button = document.getElementById("dicter");
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!button || !Recognition) return;
  const status = document.getElementById("statut-dictee");
  const field = form.elements.texte;
  button.hidden = false;
  button.addEventListener("click", () => {
    const recognition = new Recognition();
    recognition.lang = "fr-FR";
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      const text = event.results[0][0].transcript;
      field.value = field.value ? `${field.value} ${text}` : text;
      status.hidden = true;
    };
    recognition.onerror = () => {
      status.textContent = "Dictée indisponible ou refusée.";
      status.hidden = false;
    };
    status.textContent = "Parlez…";
    status.hidden = false;
    recognition.start();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  for (const removal of document.querySelectorAll("form.supprimer-conversation")) {
    removal.addEventListener("submit", (event) => {
      if (!window.confirm("Supprimer définitivement cette conversation ?")) event.preventDefault();
    });
  }
  const end = document.getElementById("fin");
  if (end && window.location.hash === "#fin") end.scrollIntoView();
  const form = document.getElementById("formulaire-assistant");
  if (!form) return;
  setupSending(form);
  setupDictation(form);
  // Réponse déjà en cours à l'ouverture de la page (rechargement, autre onglet) : on la suit.
  for (const bubble of document.querySelectorAll(".reponse.en-cours[data-tour]")) {
    form.querySelector("button[type=submit]").disabled = true;
    follow(bubble, bubble.dataset.tour, { form });
  }
});
