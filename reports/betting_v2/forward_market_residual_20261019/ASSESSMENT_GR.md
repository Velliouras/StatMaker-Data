# Νέα προαγωνιστική έκδοση μοντέλου αγοράς — 19/10/2026

Η χωριστή δοκιμή εγκαταστάθηκε και η πρώτη εκτέλεση ολοκληρώθηκε επιτυχώς. Candidate market-residual-counts-v1-oct19-2026, primary residual_conservative. Σταθερή πραγματικά δημοσιευμένη earliest parameter bank cutoff19/10, SHA256 e130715486d159285e32e3a5b849d00158ba9171d96c41cd1a91173e0fe28fdf, από pinned95721171432905456a7ce95579f034e64a7bd930. Δεν αντικαθιστά παραγωγικό/app μοντέλο.

Source5a1b886e67a2e6043ec236b3f03bd511479bda68, trigger48bfeae8f19224a496a7dfb683366a44f564666c. GitHub run39222717058 SUCCESS.
https://github.com/Velliouras/StatMaker-Data/actions/runs/39222717058

Πρώτο freeze19/10 08:27:23.552838UTC (11:27:23 Ελλάδας). Επιβεβαίωση μετά επιτυχή Git push08:27:30.959234UTC. Frozen cohort5659d22aca58305118e7f7af2c29ee00bff4a86c7f0b63d2172c9b8816915b25 (SHA256), commit δημοσίευσης4233a5e8ea4fb9c845fcca65c4bc2e2e41ca132f. Pinned verified results3df9f7a2e0a9837df917bdb1687299572bbf3424.

Η πρώτη διαθέσιμη φρέσκια παραλαβή μετά το cutoff της bank περιείχε2μελλοντικούς αγώνες,8fixture/metric packets. Οι πλήρεις πραγματικές O/U αναφορές δίνουν160προσφορές ανά μοντέλο, σε stat/market/residual. Το2είναι μέγεθος αυτής της πρώτης φρέσκιας παραλαβής, όχι η περίπου2.000fixtures ιστορική βάση εκπαίδευσης/αξιολόγησης. Διαφορετικές νέες αγορές στον ίδιο fixture επιτρέπονται αργότερα. Παλιότερες τιμές πριν από το bank cutoff δεν μπήκαν για να γεμίσει το δείγμα.

| Μοντέλο | Point επιλεγμένα | Conservative επιλεγμένα | Βαθμολογημένα |
|---|---:|---:|---:|
| stat, ίδιο διαθέσιμο universe | 2 | 1 | 0 |
| market με αρχικούς στατιστικούς guards/cap | 0 | 0 | 0 |
| residual, νέο | 0 | 0 | 0 |

Το νέο μοντέλο απέχει στις δύο αρχικές point προτάσεις. Δεν τις μετατρέπει σε picks με χαλάρωση p/odds/EV ή αναπλήρωση conservative winner. Η αποχή δεν είναι κέρδος ούτε απόδειξη καλύτερης στρατηγικής· τα160forecasts διατηρούνται για proper σύγκριση όταν έρθουν αποτελέσματα. Οι δύο παλιές ήττες της σειράς forward_market_first_20261018 παραμένουν χωριστά καταγεγραμμένες και δεν είναι οι δύο τωρινές εκκρεμείς προτάσεις.

Προγραμματισμός κάθε3ώρες, λεπτό53UTC, subject to GitHub delays. Καταγράφει νέες πρώτες επιλέξιμες παραλαβές, δημοσιεύει πριν από σέντρα και βαθμολογεί cached τελικά stats. Μηδέν provider/LLM calls ή fits στο συγκεκριμένο workflow. Ο upstream collector έχει δικό του budget και δεν ενεργοποιείται από αυτό το job. Τα coeffs παραμένουν fixed, ενώ τα lagged base history features ενημερώνονται· δεν ισχυριζόμαστε ότι μαθαίνει αυτόματα καινούργιους coefficients. Νέα freezes λήγουν01/11 00:00UTC, scoring παλιών συνεχίζει.

Επαλήθευση PASS:9δημοσιευμένα artifacts με ακριβή byte/GitSHA,164bound inputs, frozen SHA/proof, observation/update/feature/kickoff clocks, actual bank label cutoff πριν από κάθε receipt, ίδια contracts/gates και conservative ως υποσύνολο των point winners χωρίς replacement. Τα αρχικά43προϋπάρχοντα forward gzip blobs της συγκριτικής tree παρέμειναν αμετάβλητα. Καμία αλλαγή app/UAT/PROD/UI/version, καμία αυτόματη προαγωγή, κανένα pull/APK.

Η ιστορική απόδοση του challenger συγκεντρώνεται σε SHOTS Σεπτεμβρίου, οι λίγες επιλογές Οκτωβρίου έχασαν και ο matched later-price conditional δείκτης ήταν−4,59%. Αυτοί οι περιορισμοί ισχύουν μαζί με το θετικό ιστορικό ROI. Η νέα σειρά ελέγχει την αλλαγή σε επόμενα δεδομένα χωρίς να την παρουσιάζει ως αποδεδειγμένα κερδοφόρα.
