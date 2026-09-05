// Masquage des montants, appliqué AVANT le premier rendu.
//
// Chargé sans `defer` depuis le <head> : une classe posée après coup laisserait
// les montants s'afficher un instant. Dans un fichier plutôt qu'en ligne pour
// que la politique de sécurité de contenu puisse interdire tout script inline.
(function () {
  try {
    if (localStorage.getItem("pf-hide") !== "0") {
      document.documentElement.classList.add("hide-amounts");
    }
  } catch (e) {
    // Stockage local refusé (navigation privée verrouillée) : ne rien masquer
    // vaut mieux qu'une page qui ne finit pas de se construire.
  }
})();
