Tu regardes un passage d'une vidéo de gameplay Monster Hunter Wilds, capturée sur PS5, image et son. Le joueur est le chasseur que la caméra suit.

Tu analyses comme un joueur expérimenté qui revoit sa partie, action par action. Les fiches à la fin disent comment lire l'écran, l'arme et le monstre. Elles passent avant tes souvenirs du jeu.

Le passage va de {debut} à {fin}. Tous les temps sont au format MM:SS depuis le début de la vidéo, pas du passage.

Dans `lignes` : une ligne par événement, au plus une toutes les 2 s, sans trou, de {debut} à {fin}. Une attaque du monstre, un cri, un contre, un coup reçu, un changement de lame font une ligne. Pour chaque ligne :
- `t` : le temps.
- `ecran` : ce que tu lis à cet instant, recopié : action verte, message, chiffres, ou « liste vide ».
- `joueur` et `monstre` : ce que fait chacun.
- `position` : où est le chasseur par rapport au monstre, en regardant la hauteur. `contact` : à portée d'arme. `distance` : même hauteur, hors de portée. `plus bas` ou `plus haut` : ils ne sont pas au même niveau.
- `lame`, `vie` : leur état à l'écran. `alerte` : vrai si la vague orange est là.
- `issue` : `reussi` si l'action du joueur marche, `rate` si elle échoue, `subi` si le monstre lui impose quelque chose, `neutre` sinon.
- `preuve` : ce qui le montre.

Juge chaque action sur son résultat, pas sur son nom. Une attaque sans chiffres de dégâts n'a pas touché : `rate`. Une esquive ou un contre sans attaque du monstre à cet instant ne sert à rien : `neutre`, et dis-le. Note ce qui rate autant que ce qui réussit. Un `rate` ou un `subi` sans preuve n'existe pas : n'en invente aucun.

Dans `a_garder` : tous les passages qui feraient un bon clip, de toutes tailles, qui peuvent se chevaucher. Un geste fort tient en 8 à 15 s, un enchaînement en 20 à 40 s. Chaque clip commence avant l'action et finit au moins 3 s après son sommet. Ne coupe jamais une action. Pour chacun : `debut`, `fin`, `titre`, `pourquoi`.

Dans `drole` : les décalages de situation, s'il y en a. Un combat qui continue alors qu'ils ne sont pas à la même hauteur, un contre sur un monstre hors d'atteinte, le joueur qui ramasse pendant que les autres se battent. Un coup reçu ou un raté seul n'en est pas un. Aucun : liste vide. Pour chacun : `debut`, `fin`, `titre`, `pourquoi`.

Dans `doute` : ce que tu n'as pas su lire. « Je ne sais pas » est une bonne réponse.

Réponds en français, avec les noms affichés à l'écran. Pour `lame` et `vie`, recopie les faits mesurés quand il y en a : ne juge pas ces barres à l'œil.

