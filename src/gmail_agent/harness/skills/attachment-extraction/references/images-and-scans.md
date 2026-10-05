# Images, scans and unparsed binaries

Image content is not readable yet: `read_attachment` only returns the file name, type
and size.

- Describe what the image most likely is from its file name and the email context
  (for example "photo attached to a personal message", "scanned receipt").
- Say explicitly that the content was not read. Never invent text from an image.
- For other binary files (archives, Office formats other than DOCX), report the name,
  type and size, and that the file was not parsed.
