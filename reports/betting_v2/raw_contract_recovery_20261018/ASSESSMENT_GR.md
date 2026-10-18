# Raw φάουλ και κάρτες — αποτέλεσμα 18/10/2026

Ελέγχθηκαν και τα 85 προκαθορισμένα snapshots, 3.316.647.559 bytes, με αρχικό Git blob SHA1/μέγεθος και χρονικούς περιορισμούς. Επιτυχής εκτέλεση 39158957867, source a75e5cb517c22da7e4c8625b7f53e64bd5a8a78b, αποτελέσματα ce616752a99c2b997cd3058af32ded82f832c41a. Πέρασαν 13 tests. Κανένα νέο API παρόχου/LLM, fit ή αλλαγή app.

## Φάουλ

Ανακτήθηκαν πραγματικές πρώτες τιμές FOULS για 162 αγώνες αντί 20 στο παλιό whole-fixture first packet, με 1.630 offers. Οι τελευταίες παραμένουν 162 / 1.622 offers. Όλες οι ανακτημένες πλευρές είναι OVER. Σε όλο το raw inventory υπήρχαν 414.662 numeric foul offers, συμπεριλαμβανομένων player props που δεν χρησιμοποιούνται, και κανένα UNDER πεδίο. Δεν βρέθηκε χαμένη UNDER τιμή από parser. Η αξιολόγηση υπαρκτού OVER από στατιστικό μοντέλο επιτρέπεται· no-vig O/U reference χωρίς δεύτερη πλευρά δεν υπολογίζεται.

Η επιλογή παραλαβής ανά metric ανέδειξε πραγματική απώλεια δεδομένων στην προηγούμενη επιλογή μίας παραλαβής για όλο τον αγώνα. Από τις παλιές πλήρεις raw μετρήσεις: SOT 758 fixtures έναντι 209 πρώτων whole-fixture τιμών, SHOTS 454 έναντι 46. Αυτό οδήγησε σε ξεχωριστή, προδηλωμένη ανάκτηση/replay στο ../market_first_counts_20261018/ — όχι σε αλλαγή ορίων ή επιλογή αγορών από ROI.

## Κάρτες

Ανακτήθηκαν Bet365 TOTAL/TEAM card-unit offers για 763 fixtures: πρώτες 4.223, τελευταίες 3.149, με πλήρες O/U στους 757 αγώνες και στα δύο packets. Χρησιμοποιείται διακριτό BET365_CARD_UNITS, όχι το παλιό HY+HR. Ο [οδηγός settlement του παρόχου](https://docs.odds-api.io/guides/settlement) τεκμηριώνει Bookings Totals / Home / Away σε μονάδες yellow 1 / red 2 / δεύτερη κίτρινη με αποβολή +2. Οι [κανόνες Bet365](https://help.bet365.com/s/en/sportsrules/soccer/card-markets) απαιτούν επίσης σωστή συμμετοχή και περίοδο. Δεν ισχυριζόμαστε ότι το proxy/source grading πιστοποιεί bookmaker πληρωμή.

Τα 405 pinned cache αρχεία (49.326.370 bytes) περιλαμβάνουν πλήρεις fixture responses με embedded events/lineups/substitutions. Ο προηγούμενος περιορισμένος έλεγχος των 90 ξεχωριστών UEFA event files δεν κάλυπτε αυτή την πρόσθετη πηγή. Από 1.224 μοναδικές cached fixture responses, 74 πέρασαν strict label-readiness checks, 51 είχαν frozen history API fixture ID και ακριβές kickoff/name join, 38 από αυτές είχαν τις ανακτημένες τιμές καρτών. Δεν θεωρούνται όλα τα 763 fixtures εκκαθαρισμένα.

Ο αποκλεισμός κάθε αγώνα με αμφίβολη κάρτα 90+ θα εξαρτούσε το δείγμα από το αποτέλεσμα. Για αυτό δημιουργήθηκε ξεχωριστή μέθοδος συντηρητικών card count/settlement bounds, η οποία κρατά τα αμφίβολα outcomes ορατά. Δεν υπολογίστηκε ROI σε εκ των υστέρων υποσύνολο μόνο εύκολων labels. Δες ../card_settlement_bounds_20261018/summary.json.

Προετοιμάστηκε επίσης μη εκτελεσμένο native provider settlement πλάνο: 1.975 μοναδικά selections, 748 events, 40 αρχικές κλήσεις των έως 50. Δεν εκτελέστηκε provider request. Μη τεκμηριωμένο Number of Cards In Match alias δεν μετονομάστηκε αυτόματα. Δες ../card_provider_settlement_plan_20261018/OPERATIONS_GR.md.

[Πλήρη αποτελέσματα](summary.json) · [Έλεγχοι ανά fixture](card_label_readiness.json) · [Πρωτόκολλο](PROTOCOL.md)
