# Διόρθωση πιθανοτήτων στο πλήρες ανακτημένο δείγμα

Μόνο prior μήνες στην εκπαίδευση, ίδια όρια, ίδια διαθέσιμα γεγονότα στα raw/calibrated. Όλοι οι μήνες παραμένουν DEVELOPMENT. Πρώτη και τελευταία παραλαβή επικαλύπτονται.

| Σκέλος | Πρώτη: bets / ROI | Τελευταία: bets / ROI |
|---|---:|---:|
| control_raw_point | 386 / +7.33% | 400 / +7.30% |
| control_raw_conservative | 175 / -0.35% | 157 / +8.74% |
| control_calibrated_point | 265 / +12.84% | 283 / +11.06% |
| control_calibrated_conservative | 118 / +8.41% | 115 / +3.31% |
| coupled_raw_point | 425 / +7.34% | 452 / +8.58% |
| coupled_raw_conservative | 191 / +3.97% | 178 / +11.64% |
| coupled_calibrated_point | 300 / +9.94% | 324 / +12.08% |
| coupled_calibrated_conservative | 133 / +10.29% | 126 / +8.85% |

Το p>=60%, 1.80<odds<3.00, EV, guards φαβορί και winner-before-conservative παραμένουν αμετάβλητα. Το νέο decision layer δεν αυξάνει raw πιθανότητα για να δημιουργήσει επιλέξιμο bet. Η πλήρης πιθανότητα για proper scoring παραμένει συμπληρωματική OVER/UNDER με σταθερή push mass. Οι συντηρητικές ευαισθησίες δεν είναι εγγυημένα confidence bounds.

Τα πλήρη scores ανά αγορά/μήνα, οι γνωστές και άγνωστες επιλογές, τα όρια ελλειπόντων αποτελεσμάτων και ο ίδιος ακριβής μεταγενέστερος bookmaker/line έλεγχος φυλάσσονται στα artifacts. Καμία αλλαγή ενεργού app ή παλιών prospective αρχείων.
