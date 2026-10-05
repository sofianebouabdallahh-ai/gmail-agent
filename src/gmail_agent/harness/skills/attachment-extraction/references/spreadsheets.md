# CSV and spreadsheet attachments

Rows arrive as comma-separated lines; the first line is usually the header.

- State what one row represents (an order line, a transaction, a person).
- Report the row count from the `[CSV, N rows]` header (it includes the header row).
- Report totals only if a total row exists or the sum is trivially small to verify;
  otherwise list the largest or most relevant rows instead of computing.
- Keep column names exactly as written.
