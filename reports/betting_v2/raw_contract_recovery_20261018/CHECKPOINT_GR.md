# Raw ανάκτηση φάουλ/καρτών — σημείο συνέχειας 18/10/2026

Ο χρήστης αντέδρασε στο πρόωρο κλείσιμο της προηγούμενης συνέχισης. Η αρχική εντολή παραμένει: συνέχιση πραγματικής δουλειάς, χωρίς νέα επιβεβαίωση ή pull από τον χρήστη.

Υλοποιήθηκαν `raw_contract_recovery.py`, `audit_raw_contract_archives.py` και 13 tests. Αποθηκεύτηκαν πριν τον πλήρη έλεγχο: source a75e5cb517c22da7e4c8625b7f53e64bd5a8a78b. Δεσμεύτηκαν 133 SHA256 είσοδοι και 405 αρχεία API/context με Git blob SHA1 και μέγεθος (49.326.370 bytes). Τα 85 snapshots των παλιών, ήδη επιλεγμένων manifests έχουν 3.316.647.559 bytes. Κανένα ROI fit, αλλαγή ορίων ή app.

Action σε εξέλιξη: https://github.com/Velliouras/StatMaker-Data/actions/runs/39158957867 . Πριν απαντήσεις, έλεγξε πραγματική κατάσταση και summary/progress. Δεν έχει ακόμη καταγραφεί αποτέλεσμα στο παρόν checkpoint.

Στόχος 1: ανεξάρτητη ανάγνωση raw foul/card outcome fields, χωρίς εικασία missing UNDER, με πραγματικές χρονικές/ταυτοτικές δεσμεύσεις. Earliest/latest επιλογή ανά fixture/metric, ώστε υπάρχουσα πλήρης αγορά να μη χάνεται λόγω συνολικού packet χωρίς αυτό το metric. Πλήρη quotes_all και ημερήσιο resumable scan checkpoint.

Στόχος 2: έλεγχος ενσωματωμένων events/lineups/substitutions σε πλήρεις fixture responses και domestic context caches. Όχι μόνο τα 90 ξεχωριστά UEFA event files του παλιού card audit. Πρόκειται για πρόσθετες πηγές που υπήρχαν ήδη. Η δοκιμή readiness μπλοκάρει second-yellow convention, 90+ final-whistle αμφιβολία, μη γνωστή συμμετοχή, mismatch με aggregate completeness, conflicting labels. BET365_CARD_UNITS ξεχωριστό από HY+HR. Δεν θεωρούνται τα referee ονόματα των τελικών caches προαγωνιστικά features.

Μετά: επαλήθευση recovered sides/fixtures και ready labels· αν υπάρχει κοινό σύνολο καρτών με ακριβή labels και αρκετά προηγούμενα δεδομένα, ξεχωριστό χρονικό πρωτόκολλο fit/replay πριν από εκπαίδευση. Αν όχι, καταγραφή πραγματικού κενού και συνέχιση στην ανακτήσιμη πλευρά. Μην παρουσιάσεις audit ως κερδοφόρο engine.

Προηγούμενα αποτελέσματα στο ../conditional_sot_20261018/ASSESSMENT_GR.md και αντίστοιχα GAP/cross_market folders. Όλα ιστορικά DEVELOPMENT. Τα 8 frozen files των παλιών forward σειρών μένουν αμετάβλητα. Τα νέα source paths δεν αλλάζουν παλιές hash-bound πηγές. Χωρίς subagents. Python: μηδέν provider/LLM calls, η συνομιλία καταναλώνει usage. Το υπόλοιπο limit δεν είναι ορατό.
