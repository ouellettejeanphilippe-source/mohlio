# mohlio — flux RSS pour les balados OHdio

Ce dépôt génère des flux RSS pour une liste d'émissions de Radio-Canada OHdio
et les publie comme fichiers statiques, afin de pouvoir les écouter dans
n'importe quelle application de balado.

Les flux sont servis ici :
`https://ouellettejeanphilippe-source.github.io/mohlio/feed_<id>.xml`

## À quelle vitesse un nouvel épisode arrive

Le workflow demande un passage **aux 10 minutes**, et c'est le passage lui-même
qui attend. Quand une émission est attendue et que son épisode n'est pas encore
là, le générateur **revérifie toutes les 3 minutes** jusqu'à ce qu'il arrive, ou
pendant 45 minutes au maximum. En dehors de ces fenêtres, un passage dure une
quinzaine de secondes et se termine.

Un passage qui démarre **juste avant** une parution reste en place et l'attend,
au lieu de se terminer et de laisser l'épisode au passage suivant. C'est ce qui
fait la différence entre capter un épisode en quelques minutes et le capter une
demi-heure après.

### Pourquoi les deux moitiés sont nécessaires

GitHub ne livre qu'une fraction des passages planifiés demandés, et les livre en
retard. Mesures faites sur ce dépôt :

| Cadran demandé | Passages livrés |
|---|---|
| 24 créneaux par jour, denses | 5 à 11 par jour, soit 21 % à 46 % |
| 1 créneau par heure | 1 passage en 5 heures, soit 20 %, avec 36 min de retard |

Deux leçons. Demander un passage aux 10 minutes ne donne pas un délai de 10
minutes. Mais en demander moins ne les rend pas plus fiables pour autant : le
créneau horaire n'a pas été mieux livré que le cadran dense. La fraction est
simplement imprévisible, donc le seul levier sur le nombre de passages qui
arrivent est le nombre qu'on demande.

D'où les deux moitiés : demander beaucoup de créneaux, puisqu'un passage sans
rien à faire coûte une quinzaine de secondes, et faire en sorte que chaque
passage livré couvre toute la fenêtre autour d'une parution plutôt qu'un seul
instant.

### Heures de parution suivies

Ces heures viennent de l'historique de publication de chaque flux. Elles
servent **uniquement** à décider quand surveiller de près. Elles ne
conditionnent jamais la vérification : chaque passage vérifie les dix
émissions, donc une émission qui change d'horaire est quand même captée.

| Émission | Jours | Heure (HE) | Fenêtre |
|---|---|---|---|
| À la une | lun-ven | 05:00 | 2 h |
| Ça s'explique | mar-jeu, sam | 05:00 | 3 h |
| Olivier Niquet 24/7 | lun-ven | 08:00 | 2 h 30 |
| Décrypteurs | ven | 11:00 | 2 h 30 |
| Pouvez-vous répéter la question? | sam | 13:00 | 2 h |
| La journée (est encore jeune) | lun-sam | 14:25 | 2 h |
| Changement de ligne | mer-jeu | 15:00 | 3 h |
| Moteur de recherche | lun-jeu | 19:00 | 2 h |

Le bêtisier et Tellement hockey paraissent de façon irrégulière : ils n'ont pas
d'horaire et sont captés par la vérification horaire.

Les fenêtres sont calculées en heure de l'Est réelle par Python, donc le
changement d'heure est géré sans aucune arithmétique de dates en shell.

Si une émission rate deux parutions attendues d'affilée, le journal le signale
au lieu de servir silencieusement un flux périmé.

## Comment un flux est construit

Trois sources sont combinées pour chaque émission. Aucune n'est complète à
elle seule, alors elles sont fusionnées au lieu de se remplacer :

1. **Le fichier `feed_<id>.xml` déjà dans le dépôt.** L'API amont n'expose
   qu'une fenêtre glissante d'épisodes récents, et pour certaines émissions un
   seul épisode périmé. Le fichier au dépôt est donc le seul endroit où
   l'historique complet existe. Il sert aussi de cache des adresses déjà
   résolues : un passage sans nouveauté ne fait aucun appel de validation.
2. **La page OHdio de l'émission.** C'est la source qui voit un nouvel épisode
   en premier, quelques minutes après sa parution, mais elle ne donne qu'un
   identifiant média à résoudre vers une adresse HLS.
3. **Le document RSS du balado.** Il est en retard sur la page, mais il porte
   les MP3 progressifs, les tailles réelles et les métadonnées de l'émission.

### Ce que la fusion garantit

- Un épisode garde le **même `<guid>` toute sa vie**, même quand son fichier
  passe de HLS à MP3. Aucun doublon n'apparaît dans votre application.
- Un flux **ne perd jamais d'épisodes** parce qu'une source était lente,
  partielle ou en panne.
- Les fichiers sont écrits de façon **atomique** et **seulement quand leur
  contenu a réellement changé**, donc un passage sans nouveauté ne produit
  aucun commit.
- Le **MP3 l'emporte toujours sur le HLS** pour un même épisode, parce que
  plusieurs applications de balado ne savent pas lire un `.m3u8`.

## Mise à jour manuelle

Pour forcer une mise à jour immédiate, allez sur la
**[page du workflow](https://github.com/ouellettejeanphilippe-source/mohlio/actions/workflows/update.yml)**,
cliquez sur **Run workflow**, puis sur le bouton vert **Run workflow**.

## Utilisation locale

```bash
pip install -r requirements.txt

python main.py                      # met à jour tous les flux, une seule passe
python main.py --watch               # puis attend les épisodes attendus
python main.py --shows niquet        # une seule émission
python main.py --dry-run             # construit et valide sans rien écrire
python main.py --verbose             # journalisation de débogage
```

`--watch` accepte `--watch-minutes` (durée maximale de l'attente) et
`--poll-seconds` (délai entre deux vérifications).

`--dry-run` est aussi ce que le workflow exécute sur une pull request : les
flux sont construits et validés, mais jamais écrits.

## Tests

```bash
python -m unittest -v
```

Les tests couvrent les garanties ci-dessus : stabilité des `guid`,
dédoublonnage des deux versions d'une même diffusion, refus de rétrécir un
flux, et écriture uniquement en cas de changement réel.

## Prérequis

- Python 3.11
- `requests`

## Journal de la dernière mise à jour

<!-- RUN_LOG_START -->
Last update: 2026-09-12 18:18 UTC

### Feeds

- ✅ [betisier](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6327.xml) — 10 episodes, latest 2025-12-29 06:00 ET
- ✅ [changement](https://ouellettejeanphilippe-source.github.io/mohlio/feed_13061.xml) — 30 episodes, latest 2026-06-18 15:00 ET
- ✅ [decrypteurs](https://ouellettejeanphilippe-source.github.io/mohlio/feed_11099.xml) — 50 episodes, latest 2026-09-11 11:00 ET
- ✅ [explique](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6108.xml) — 51 episodes, latest 2026-09-12 06:00 ET
- ✅ [hockey](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6104.xml) — 51 episodes, latest 2026-09-11 15:30 ET
- ✅ [journee](https://ouellettejeanphilippe-source.github.io/mohlio/feed_9887.xml) — 52 episodes, latest 2026-09-12 07:00 ET
- ✅ [niquet](https://ouellettejeanphilippe-source.github.io/mohlio/feed_12095.xml) — 50 episodes, latest 2026-09-11 08:30 ET
- 🆕 [question](https://ouellettejeanphilippe-source.github.io/mohlio/feed_7791.xml) — 51 episodes, latest 2026-09-12 13:00 ET
- ✅ [recherche](https://ouellettejeanphilippe-source.github.io/mohlio/feed_6056.xml) — 52 episodes, latest 2026-09-11 12:30 ET
- ✅ [une](https://ouellettejeanphilippe-source.github.io/mohlio/feed_302.xml) — 350 episodes, latest 2026-09-11 05:06 ET

### Warnings

- `betisier`: podcast RSS unavailable (podcast RSS returned no channel)
- `changement`: podcast RSS unavailable (podcast RSS returned no channel)
- `changement`: no new episode for the last 3 expected publications
- `decrypteurs`: podcast RSS unavailable (podcast RSS returned no channel)
- `hockey`: podcast RSS unavailable (podcast RSS returned no channel)
- `niquet`: podcast RSS unavailable (podcast RSS returned no channel)
<!-- RUN_LOG_END -->
