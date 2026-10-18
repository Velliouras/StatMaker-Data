# Σημείο συνέχισης — 18/10/2026

Ο χρήστης ζήτησε συνέχιση της εργασίας όσο επιτρέπει το όριο. Δεν είναι ορατό από τον assistant το υπόλοιπο ChatGPT/Codex usage και δεν δόθηκε εγγύηση κέρδους.

## Ολοκληρώθηκε μετά το αρχικό handoff

- Επαναφέρθηκε το προσωρινό checkout που καθαρίστηκε από συντήρηση. Τα νέα αρχεία πλέον δημοσιεύονται πριν από βαριές εκτελέσεις.
- Υλοποιήθηκαν goal-price anchors από πραγματικά 1Χ2/γκολ O/U και νέος joint count πυρήνας κόρνερ, με δυναμική διασπορά και constant-dispersion ablation.
- Πέρασαν 20 έλεγχοι. Ολοκληρώθηκε το [Action 39153226773](https://github.com/Velliouras/StatMaker-Data/actions/runs/39153226773).
- Το cross-market conservative απέδωσε −4,14% / 47 bets earliest και −8,65% / 97 latest. Μικρή βελτίωση count scores δεν έδωσε κερδοφόρες επιλογές. **Δεν προάγεται**. [Πλήρης αξιολόγηση](../cross_market_counts_20261018/ASSESSMENT_GR.md).
- Επαληθεύτηκαν τα payouts/pushes, το dedup και τα όρια όλων των priced rows και των δύο packets. Τα 8 immutable αρχεία των forward σειρών παρέμειναν ίδια.

## Υλοποιήθηκε και ολοκληρώθηκε επιτυχώς

- `gap_ratings.py`: τέσσερις attack/defence home/away αξιολογήσεις για SHOTS/SOT/CORNERS, prior MAE fit, prefix cold priors, batching ανά ημέρα.
- `gap_goal_rates.py`: prior shared Poisson goal-rate offsets από τις προβλεπόμενες παραγωγές, διατήρηση των frozen rate banks.
- `research_gap_attack.py`: μηνιαίες προηγούμενες προσαρμογές GAP, features για όλα τα διαθέσιμα ιστορικά fixtures της περιόδου και ξεχωριστά για πραγματικές odds παραλαβές.
- `research_gap_goals.py`: κοινό priced σύνολο TOTAL/TEAM goals και BTTS, statistical/market/GAP, σταθερός winner και προδηλωμένες πολιτικές strong60 και ev55_research.
- Η ev55 είναι μόνο ερευνητική, **όχι STRONG/SOLID ή αλλαγή εφαρμογής**. Ίδια odds >1,80/<3,00 και ίδιο favorite-scenario floor 60%.
- Πέρασαν τοπικά 18 έλεγχοι: 8 GAP, 5 goal-rate και 5 replay. Δεσμεύτηκαν 122 είσοδοι.
- Source: `4e206e19f46a1af23cc4fa1cfd470db1b82f1fa7`, trigger: `9ef351d8288e8d2b072585a2e1da7c35739cc75c`.
- [Action 39154279939](https://github.com/Velliouras/StatMaker-Data/actions/runs/39154279939). Επιτυχής εκτέλεση, αποτελέσματα commit `d6ecb4a9d9170d4a7a88cea69ba7f3d7c96c6097`.
- Workflow αποθηκεύει μηνιαίες παραμέτρους, daily checkpoint/features και τελικά reports, ακόμη και σε αποτυχία/timeout. Το resume απαιτεί ίδιο manifest signature.

## Έλεγχος δεδομένων για επόμενες αγορές

Στο frozen history 17.950 fixtures υπάρχουν **13.422** με ταυτόχρονα SHOTS/SOT. Δεν βρέθηκε καμία περίπτωση όπου SOT > shots σε μία από τις ομάδες. Αυτό επιτρέπει επόμενη συνεπή conditional-shot μελέτη, χωρίς να αποδεικνύει από μόνο του κερδοφορία.

Το συγκεκριμένο normalized frozen history δεν περιέχει πεδίο διαιτητή. Δεν σημαίνει ότι όλα τα raw caches του app στερούνται διαιτητών. Πριν χρησιμοποιηθεί τέτοια μεταβλητή χρειάζεται ακριβής σύνδεση και απόδειξη ότι ήταν διαθέσιμη πριν από την πρόβλεψη.

## Τελευταίο αποτέλεσμα και επόμενα

Το GAP 60% conservative απέδωσε +7,40% / 42 earliest και +5,49% / 45 latest. Μεγάλα διαστήματα αβεβαιότητας· latest Οκτωβρίου −5,16% / 14. EV55 latest −1,37% / 149. Ο έλεγχος όλων των 24 report sets επιβεβαίωσε payouts, pushes, όρια, dedup και κέρδη. Πρόσθετος έλεγχος GAP έναντι intercept αναπαρήγαγε proper scores: συνολική υπεροχή αβέβαιη. Δες [τελική αξιολόγηση](ASSESSMENT_GR.md).

Συνέχεια: υλοποίηση ξεχωριστού μοντέλου SOT υπό SHOTS με beta-binomial μετασχηματισμό, όλα τα έγκυρα prior ζεύγη, χρονολογική αξιολόγηση, χωρίς ROI tuning. Αποθήκευση πηγών και πρωτοκόλλου πριν από πραγματικό fit. Χωρίς subagents. App/UAT/PROD και προηγούμενα frozen αρχεία μένουν αμετάβλητα.

Όλα τα Python experiments: 0 provider calls, 0 LLM calls, κανένα στοίχημα. Οι συνομιλίες/tool calls καταναλώνουν usage. Δεν έχει αποδειχθεί σταθερή κερδοφορία.
