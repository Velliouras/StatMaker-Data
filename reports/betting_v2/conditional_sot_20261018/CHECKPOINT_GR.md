# Συνέχεια conditional SOT — 18/10/2026

Η έρευνα υλοποιήθηκε και ξεκίνησε στο GitHub Actions. Πηγές και πρωτόκολλο αποθηκεύτηκαν πριν από πραγματικό fit: `8db76ff3d16418baf74b5e432befa2c36d01cb53`. Πέρασαν 15 tests και πρόσθετος έλεγχος 8 ημερών της χρονικής υστέρησης training features. Δεσμεύτηκαν 127 είσοδοι SHA256.

Κύρια πολιτική: conditional_conservative, p≥60%, odds αυστηρά >1,80/<3,00, θετικό push-aware EV, ίδιοι κανόνες φαβορί/κατάταξης, ένας νικητής ανά αγώνα/SOT οικογένεια, αποχή χωρίς αντικατάσταση όταν αποτύχει η συντηρητική εκτίμηση. Δεν υπάρχει νέο EV55 σκέλος. Συγκρίνονται statistical / market / conditional, point / conservative, σε κοινές πραγματικές TOTAL/TEAM SOT αγορές. Όλοι οι ήδη ελεγμένοι μήνες είναι DEVELOPMENT.

Η εκτέλεση εκπαιδεύει conversion intercept, home venue και beta-binomial concentration σε όλα τα έγκυρα prior ζεύγη, με 24ωρη υστέρηση αποτελεσμάτων και prefix venue/team/group shrinkage. Παγωμένος μηνιαίος joint SHOTS γονέας, όχι actual shots του υπό πρόβλεψη αγώνα. Ο μετασχηματισμός εγγυάται SOT≤SHOTS ως μαθηματική συνέπεια, όχι κερδοφορία.

Έλεγξε πρώτα την πραγματική κατάσταση του workflow `betting-v2-conditional-sot.yml` και τα `summary.json`, `parameter_banks.json`, `priced_earliest.json`, `priced_latest.json`. Μην αναφέρεις αποτελέσματα πριν τα δεις. Το workflow αποθηκεύει fit banks, training features και daily replay checkpoints ακόμη σε αποτυχία. Το resume επιτρέπεται μόνο με ίδιο binding signature.

Μετά την ολοκλήρωση: επαλήθευση ορίων/dedup/payouts, proper scores όλων των αγορών, μήνες και διαστήματα αβεβαιότητας. Αποθήκευση ASSESSMENT_GR.md και ενημέρωση αυτού του checkpoint. Μην επιλέξεις εκ των υστέρων μόνο την κερδοφόρα αγορά ή αλλάξεις όρια από το ROI.

GAP τελική αξιολόγηση: `../gap_attack_model_20261018/ASSESSMENT_GR.md`, commit `9ab7c77f310e0a7df06d19e0a5e2cfe48dc7c01a`. GAP κύριο historical ROI +7,40%/42 earliest και +5,49%/45 latest, με μεγάλη αβεβαιότητα, latest Οκτωβρίου −5,16%/14. EV55 latest −1,37%/149. Πρόσθετη συνολική πληροφορία GAP πέρα από απλό intercept παραμένει αβέβαιη. Cross-market κόρνερ απέτυχε (−8,65%/97 latest).

Εντολή χρήστη: συνέχιση όσο επιτρέπει το όριο, χωρίς νέο pull/εγκατάσταση. Δεν επιτρέπονται subagents. Δεν έχουν αλλάξει app/UAT/PROD, όρια STRONG ή προηγούμενες παγωμένες forward δοκιμές. Python 0 provider/LLM calls, η συνομιλία καταναλώνει ξεχωριστό usage. Δεν έχει αποδειχθεί σταθερή κερδοφορία.
