// Enregistrement du service worker (fichier séparé : aucun script en ligne, CSP stricte).
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" });
}
