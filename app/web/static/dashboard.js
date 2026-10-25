// Graphiques du tableau de bord (Chart.js embarqué). Les données arrivent en centimes ;
// la conversion en euros ne sert qu'à l'affichage.
document.addEventListener("DOMContentLoaded", () => {
  const source = document.getElementById("donnees-graphiques");
  if (!source || typeof Chart === "undefined") return;
  const d = JSON.parse(source.textContent);
  const euros = (cents) => cents / 100;
  const format = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
  // Couleurs de la feuille de style (thème clair ou sombre, selon le téléphone).
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  Chart.defaults.color = color("--discret");
  Chart.defaults.borderColor = color("--bord");
  // Palette distincte pour les catégories, lisible sur fond clair comme sombre.
  const palette = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"];
  const tooltip = { callbacks: { label: (ctx) => `${ctx.dataset.label || ctx.label} : ${format.format(ctx.parsed.y ?? ctx.parsed)}` } };
  new Chart(document.getElementById("graphique-flux"), {
    type: "bar",
    data: {
      labels: d.mois,
      datasets: [
        { label: "Recettes", data: d.recettes.map(euros), backgroundColor: color("--positif") },
        { label: "Dépenses", data: d.depenses.map(euros), backgroundColor: color("--negatif") },
      ],
    },
    options: { maintainAspectRatio: false, plugins: { tooltip }, scales: { y: { ticks: { callback: (v) => format.format(v) } } } },
  });
  if (d.categories.length) {
    new Chart(document.getElementById("graphique-categories"), {
      type: "doughnut",
      data: { labels: d.categories, datasets: [{ data: d.montants_categories.map(euros), backgroundColor: d.categories.map((_, i) => palette[i % palette.length]), borderColor: color("--surface") }] },
      options: { maintainAspectRatio: false, plugins: { tooltip, legend: { position: "right" } } },
    });
  }
});
