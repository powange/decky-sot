# Decky SoT – plugin Decky Loader pour Sea of Thieves

Des aides pour Sea of Thieves sur le Steam Deck, affichées en jeu via [Decky Overlay](https://github.com/powange/decky-overlay).

## Tempête

Choisis jusqu'à 12 îles : l'overlay affiche pour chacune dans combien de temps les **premières pluies** y arrivent.

- **Compte à rebours** : la pluie arrive sur l'île dans les 2 prochaines heures.
- **Pluie, fin HH:MM** : il pleut sur l'île en ce moment, jusqu'à l'heure indiquée.
- **Pas de tempête** : pas de pluie prévue dans les 2 h (option pour masquer ces îles).

### Comment c'est calculé

La tempête suit le même trajet sur tous les serveurs. Les prévisions viennent de l'API de [Merfolk's Lullaby](https://www.merfolkslullaby.com/map?tab=weather), comme le bouton Tempête de [streamdeck-sot](https://github.com/powange/streamdeck-sot). Chaque point donne l'heure et la position du **centre** de la tempête au-dessus d'une île, environ 2 h à l'avance.

1. Entre deux points, le centre avance en ligne droite, avec la même accélération que l'animation de leur carte (elle dépend de la vitesse).
2. La pluie est un disque de la taille de la tempête dessinée sur leur carte (environ 650 m de rayon), centré sur cette position.
3. Les premières pluies sur une île, c'est le moment où ce disque touche le contour de l'île (contours fournis par la même API).

Le plugin ne demande les prévisions qu'une fois toutes les 24 minutes (comme leur carte), et seulement quand c'est utile : Sea of Thieves lancé, ou menu du plugin ouvert. Rien n'est lu dans le jeu.

### Calage

Le site et le jeu peuvent être décalés de plusieurs minutes. Section **Calage** du plugin :

- **La pluie commence sur …** : appuie sur le bouton de l'île au moment où la pluie y commence en jeu. Le décalage se règle tout seul.
- **Décalage** : réglage manuel, de -30 à +30 min (positif : la tempête arrive plus tard en jeu que sur le site).
- **Taille de la pluie** : de 50 à 400 % de la tempête dessinée par Merfolk's Lullaby, si la pluie commence plus tôt ou plus tard que prévu selon la façon dont la tempête aborde l'île.

## Réglages

- Afficher l'overlay, seulement quand Sea of Thieves est lancé (détecté par son processus `SoTGame.exe`), position.
- Compte à rebours toujours visible ou seulement dans les N dernières minutes.
- Tri par heure d'arrivée ou dans l'ordre choisi.
- Aperçu de 10 s, bouton **Actualiser la prévision**.

L'overlay ne réagit pas au toucher : Decky Overlay laisse passer tous les appuis vers le jeu. Les réglages se font dans le menu du plugin (bouton « … »), y compris en jeu.

## Installation

Decky › Paramètres › Développeur › Installer un plugin depuis une URL :

```
https://github.com/powange/decky-sot/releases/latest/download/decky-sot.zip
```

Au premier lancement, le plugin propose d'installer Decky Overlay s'il manque. Les mises à jour se font ensuite avec le bouton **Mises à jour** du plugin.

## Développement

```bash
pnpm install && pnpm run build
pip install pytest && python -m pytest tests
```

Pour publier, augmente `version` dans `package.json` et pousse sur `main` : la CI lance les tests puis publie la release.

## Licence

BSD-3-Clause
