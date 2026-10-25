// Service worker minimal : rend l'application installable (UI-01).
// Volontairement AUCUN cache : les données financières ne doivent pas rester sur le téléphone.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
  // Pas d'interception : le navigateur va toujours au réseau (via Tailscale).
});
