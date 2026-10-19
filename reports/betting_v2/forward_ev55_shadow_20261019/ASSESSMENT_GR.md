# Προδημοσιευμένη ερευνητική πολιτική EV55 — 19/10/2026

Η νέα χωριστή πολιτική εγκαταστάθηκε και εκτελέστηκε επιτυχώς. Candidate market-residual-fixed-winner-ev55-research-v1. Κύριο residual_conservative με0.55 minimum global win probability, θετικό εκτιμώμενο EV,1.80<odds<3.00 και όλους τους αρχικούς στατιστικούς/favourite guards. Ο ήδη επιλεγμένος raw point winner μένει σταθερός πριν τη διόρθωση· μετά εφαρμόζεται conservative abstention, χωρίς αναπλήρωση. Το STRONG60 του app και της προηγούμενης δοκιμής δεν αλλάζει. Κάθε55row είναι RESEARCH_EV55 και certifiedStrong=false.

Source d9567a34df41c9f07fb252a466220c3112fa23f3. Trigger5278c031d3f5fce19b99157e2999671befe0149a. GitHub run39225668871 SUCCESS.
https://github.com/Velliouras/StatMaker-Data/actions/runs/39225668871

Αντλεί τις ήδη SHA-bound προαγωνιστικές πιθανότητες της parent σειράς forward_market_residual_20261019 και την ίδια ακριβή published cutoff19/10 earliest bank. Δεν επανυπολογίζει τα base count μοντέλα και δεν κάνει fits ή provider/LLM calls· εφαρμόζει την ήδη σταθερή διόρθωση και τους κανόνες επιλογής. Έχει170source/input bindings. Αποκλείει ήδη ξεκινημένους/παλιούς source αγώνες και απαιτεί δική της δημοσίευση πριν από τη σέντρα. Δεν αποδίδει αναδρομική prospective ιδιότητα στο post-hoc55 ιστορικό.

Πρώτο freeze19/10 08:51:54.365140UTC, επιβεβαίωση08:52:02.151172UTC (11:52:02 Ελλάδας), αφού πέτυχε το push. Η parent δημοσίευση είχε προηγηθεί08:27:30.959234UTC. SHA256 νέου frozen snapshot feff1ebdc1725564bf1093df366f12ccfe12ad5ae8524ac8543b960697fd7f7b, commit b8abc2ea8e04907468fb71dd5040b7510c51ab48. Pinned audited state6b6aa8dd5f5f45af9b8d376888ce0aedef7f3ad8.

Πρώτη παραλαβή:2μελλοντικοί αγώνες,6fixture/metric partitions με διαθέσιμη πραγματική O/U αναφορά,160πλήρη forecasts ανά μοντέλο. Διατηρούνται οι ίδιες προβλέψεις και οι αρχικοί νικητές για συγκρίσεις.

| Πολιτική | Point | Conservative | Βαθμολογημένα |
|---|---:|---:|---:|
| raw stat60 comparator | 2 | 1 | 0 |
| pure market55 με raw guards/cap | 0 | 0 | 0 |
| residual EV55 research | 2 | 1 | 0 |

Η ερευνητική έκδοση έχει ήδη1συντηρητική καταγεγραμμένη πρόταση και2point προτάσεις πριν από τα αποτελέσματα. Δεν είναι πραγματικά εκτελεσμένα στοιχήματα και δεν έχει ακόμη πραγματοποιημένο ROI. Η0.60 parent residual πολιτική απείχε και στις δύο, επειδή οι νέες point εκτιμήσεις ήταν59,65%/56,58% στο1.81/1.98, με μοντελοποιημένο point EV+7,97%/+12,04%. Πιθανότητα και value είναι διαφορετικά κριτήρια· το55 δεν εγγυάται καλύτερο κέρδος.

Το parent συνεχίζει με το σταθερό δικό του schedule ανά3ώρες. Το EV55 job ενεργοποιείται μόνο μετά από επιτυχή ολοκλήρωση εκείνου (workflow_run) και δεν προσθέτει ανεξάρτητο cron ή provider fetch. Δημιουργεί projections μόνο για νέα αμετάβλητα parent cohorts, μία φορά ανά source. Έπειτα τα παλιά snapshots βαθμολογούνται από τα cached πραγματικά stats. Η πηγή των source forecasts και των forecasts/stat guards δεν τροποποιείται. Τα αρχικά43προϋπάρχοντα forward gzip blobs έμειναν αμετάβλητα.

Επαλήθευση PASS:16δημοσιευμένα ιστορικά/forward artifacts επαληθεύτηκαν byte count/GitSHA από pinned commit.170bindings, source/projection/publication chain, immutable hashes, strict pre-kickoff timing, original winner πριν από market correction, min probability ανάμοντέλο και positive-EV/odds/favourite guards, conservative ως υποσύνολο point χωρίς replacement.4νέοι integration tests PASS για positive55projection, idempotence, μη μεταβολή parent, bad SHA και missing/future-dated publication rejection.

Το ιστορικό conservative55 έδωσε91/+18,00% και65/+16,21% σε επικαλυπτόμενα DEVELOPΜENT packets. Τα95% διαστήματα περιλαμβάνουν0 και ο μεταγενέστερος conditional δείκτης τιμής μετά γκανιότα ήταν−3,97%. Αυτό στηρίζει περαιτέρω έλεγχο, όχι ισχυρισμό αποδεδειγμένης κερδοφορίας. Νέα freezes λήγουν01/11 00UTC· scoring παλιών συνεχίζει. Καμία αλλαγή app/UI/UAT/PROD ή actual stake/bet execution.
