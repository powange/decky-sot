# Decky SoT – plugin Decky Loader pour Sea of Thieves

Des aides pour Sea of Thieves sur le Steam Deck, affichées en jeu via [Decky Overlay](https://github.com/powange/decky-overlay).

## Tempête

Choisis jusqu'à 12 îles : l'overlay affiche pour chacune dans combien de temps la tempête y passe.

- **Compte à rebours** : la tempête arrive sur l'île dans les 2 prochaines heures.
- **Tempête !** : le centre de la tempête est sur l'île en ce moment.
- **Pas de tempête** : rien de prévu dans les 2 h (option pour masquer ces îles).

La tempête suit le même trajet sur tous les serveurs. Les prévisions viennent de l'API de [Merfolk's Lullaby](https://www.merfolkslullaby.com/map?tab=weather), comme le bouton Tempête de [streamdeck-sot](https://github.com/powange/streamdeck-sot) : elles donnent l'heure à laquelle le **centre** de la tempête passe sur une île, environ 2 h à l'avance. Le plugin ne les demande qu'une fois toutes les 24 minutes (comme leur carte), et seulement quand c'est utile : Sea of Thieves lancé, ou menu du plugin ouvert. Rien n'est lu dans le jeu.

Une île que la tempête frôle sans que son centre passe dessus n'apparaît pas dans les prévisions : elle reste à « Pas de tempête ».

## Réglages

- Afficher l'overlay, seulement quand Sea of Thieves est lancé (détecté par son processus `SoTGame.exe`), position.
- Compte à rebours toujours visible ou seulement dans les N dernières minutes.
- Tri par heure d'arrivée ou dans l'ordre choisi.
- Aperçu de 10 s, bouton **Actualiser la prévision**.

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
