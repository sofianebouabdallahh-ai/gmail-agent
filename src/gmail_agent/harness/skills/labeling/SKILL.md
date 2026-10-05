---
name: labeling
description: The label taxonomy - which Gmail labels exist and what belongs under each. Load before choosing the label action.
labels: [Finance, Receipts, Travel, Insurance, Health, Work, Shopping, Newsletters, Calendar, Personal]
---

# Labeling

Use only the labels below, spelled exactly. Any other label is rejected by code and the
email is sent to the owner instead. One or two labels per email.

| Label | What belongs there |
|---|---|
| `Finance` | Invoices, bank and card statements, tax documents, payment confirmations. |
| `Receipts` | Purchase receipts and order confirmations with an amount paid. |
| `Travel` | Flight, train and hotel bookings, boarding passes, check-in confirmations, delays. |
| `Insurance` | Policies, ID cards, renewals, claims. |
| `Health` | Appointments, results, prescriptions, health insurance correspondence. |
| `Work` | Anything about the owner's job, clients or colleagues. |
| `Shopping` | Shipping and delivery updates for purchases. |
| `Newsletters` | Subscribed newsletters worth keeping (otherwise `archive`). |
| `Calendar` | Invitations and event updates (also consider `schedule`). |
| `Personal` | Family and friends. |

A receipt for a flight is both `Travel` and `Receipts`.
