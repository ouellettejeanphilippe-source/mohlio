# mohlio — flux RSS pour les balados OHdio

Ce dépôt génère des flux RSS pour une liste d'émissions de Radio-Canada OHdio
et les publie comme fichiers statiques, afin de pouvoir les écouter dans
n'importe quelle application de balado.

Les flux sont servis ici :
`https://ouellettejeanphilippe-source.github.io/mohlio/feed_<id>.xml`

## À quelle vitesse un nouvel épisode arrive

Les flux sont rafraîchis **toutes les 10 minutes** pendant toute la journée de
diffusion (04 h à 21 h, heure de l'Est) et toutes les heures la nuit. Un
nouvel épisode apparaît donc dans votre application au prochain passage, soit
une dizaine de minutes après sa parution.

La fenêtre couvre les heures de parution réelles des émissions suivies :

| Heure (HE)  | Émissions                                    |
|-------------|----------------------------------------------|
| 05:00-05:06 | À la une, Ça s'explique                       |
| 08:00-09:00 | Olivier Niquet 24/7                           |
| 11:00-12:00 | Décrypteurs (vendredi)                        |
| 13:00       | Pouvez-vous répéter la question? (samedi)     |
| 14:30       | La journée (est encore jeune)                 |
| 15:00-17:00 | Changement de ligne, Tellement hockey         |
| 19:06       | Moteur de recherche                           |

Le cadran est exprimé en UTC et couvre volontairement HNE et HAE, ce qui
élimine tout ajustement au changement d'heure.

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

python main.py                      # met à jour tous les flux
python main.py --shows niquet       # une seule émission
python main.py --dry-run            # construit et valide sans rien écrire
python main.py --verbose            # journalisation de débogage
```

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
<!-- RUN_LOG_END -->
