# Methodology

Version 0.1.0 · [Türkçe](METHODOLOGY.tr.md) · [Results](RESULTS.md) · [README](../../README.md)

This document describes what the benchmark sends to Data Mask, what it records, how each metric is computed and what the numbers can and cannot tell you. The code is the reference: every rule below names the module that implements it.

## 1. What is tested

The benchmark tests the hosted Data Mask service through the official Python SDK, the way a customer would call it:

```python
tagged = client.anonymize(text, persist=False)  # masked text and the mapping
restored = client.restore(tagged.state, tagged.text)
```

It answers five questions:

1. Does the original text come back exactly?
2. Do identifiers that can be proven to be identifiers survive in the masked text?
3. Is a value that was masked once masked everywhere in the document?
4. Is there personal data the service did not notice at all?
5. How much of the legal context does the model still see?

It also records how long each call took and whether it succeeded.

## 2. Corpus

The corpus is a set of Turkish court files. Each case file has one petition, drafted with the help of AI, and the text of the documents attached to it: hearing minutes, reasoned decisions, expert reports, indictments, statements, earlier petitions, and text read from photographs and scanned pages.

| | |
|---|---:|
| Case files | 338 |
| Documents | 1,410 |
| Petitions | 338 |
| Attachments | 1,072 |
| Characters | 18,932,425 |
| Median document size | 9.8 kB |
| Largest document | 375 kB |

Petitions cover civil, criminal, labour, administrative, enforcement, consumer, notarial and tax matters. The attachment texts were extracted from UYAP `.udf` files, PDF, Word, Excel and scanned images, so they include the line breaks and recognition errors of real extracted text. The exact breakdown is in `results/full/summary.json` under `corpus`.

Sixteen files shorter than 200 bytes were left out. They are fragments of a line or two, mostly text read from a photograph.

Of these 1,410 documents, 1,396 were processed. One document of 332,387 characters reached the 120-second processing limit of the service and failed with HTTP 504. After that, documents above 80 kB were not sent: 13 more documents, about 1.9 million characters. One document above that size had been processed earlier and is included.

The documents are not part of this repository. Published results refer to a document by the first 12 hex digits of the SHA-1 of its relative path, so they carry no file names (`corpus.py`).

## 3. Protocol

Each document goes through the same steps (`runner.py`):

1. The file is read as UTF-8 and sent whole, as plain text, in one `anonymize` call.
2. The masked text and the mapping are saved locally.
3. The masked text is sent to `restore` together with the mapping.
4. The restored text is saved locally.

The settings of the published run:

| Setting | Value |
|---|---|
| SDK | `bb-mask` 0.3.1 |
| Service | `https://app.bbmask.co` |
| `persist` | `False`: the service stores nothing and returns the mapping in the response |
| `placeholder_style` | `angle` (`<PERSON_0>`) |
| `keep_legal_form`, `health_data`, `llm_review` | off (the defaults) |
| Concurrency | 1: one request at a time |
| Client timeout | 150 s (400 s in the first part of the run) |
| Pause | 3 s after each document, in most of the run |
| Largest document sent | 80 kB (see section 2) |

The run was made in several sessions. A stopped run is resumed with the same run ID, and a document that succeeded is not sent again.

**Timing.** Latency is wall-clock time around the SDK call, measured on the client. It includes the network. The run was made from one machine on an ordinary internet connection, so the numbers describe what a caller experiences, not the processing time inside the service.

**Retries.** A call is repeated only after HTTP 429, 502 or 503, at most once in the published run. A timeout (504) is not retried, because the service may have completed the request. A document counts as failed if its last attempt failed.

**Restore input.** `restore` receives the complete masked document. A model's answer normally contains only some of the placeholders, so this is the strictest input: every placeholder in the document has to resolve.

## 4. Metrics

All metrics are computed offline from the saved files (`metrics.py`). Changing a metric never needs another call to the service.

Before the masked text is searched, every placeholder is removed from it. A value therefore cannot be "found" inside a placeholder.

### 4.1 Round trip

A document passes if the restored text is equal to the original, character for character. Documents that differ only in whitespace are counted separately in `summary.json` and still fail this test.

The report also counts placeholders in the masked text that are missing from the mapping. Such a placeholder would remain in the restored text as a raw tag.

### 4.2 Provable identifiers

There are no hand-made labels, so the benchmark uses identifiers that can be recognised from the text with very little doubt (`detectors.py`):

| Identifier | Counted when |
|---|---|
| National ID number | 11 digits, first digit not 0, both check digits correct |
| IBAN | `TR` followed by 24 digits, solid or in groups, passing the ISO 13616 mod-97 check |
| E-mail address | address syntax |
| Mobile number | Turkish mobile format: `05xx…`, `+90 5xx…`, `0 (5xx)…` |
| Licence plate | province code, 1 to 3 letters, 2 to 4 digits, directly followed by the word “plaka” |

Each detector runs on the original and on the masked text. Values are normalised first, so `TR67 0001 …` and `TR670001…` are the same value. For each value, the number left is the smaller of its count in the original and its count in the masked text.

Every value left is a leak. No annotator judgement is involved, which is why this is the headline privacy figure. It is also narrow: it says nothing about names, addresses or organisations.

A random 11-digit number passes the national ID check about once in a hundred times, so a small share of the counted national IDs may be other numbers. This does not change what a leftover means: a number with a valid ID checksum was left readable.

### 4.3 Consistency of masking

With `persist=False` the service returns every value it masked. For each of those values the benchmark counts its occurrences in the original and in the masked text. A match must be a whole token; whitespace between words may be any run of spaces or line breaks.

- **Left readable** counts matches of the value exactly as written.
- **Left readable (other letter case)** repeats the search ignoring case, using Turkish case rules (`I`/`ı`, `İ`/`i`). It is limited to values with more than one word or with a digit. A single name such as “Deniz” or “Güven” is also an ordinary word, and matching it without regard to case would count ordinary words as leaks.

Values shorter than four characters are skipped. `JOB_TITLE` is left out, because its values (“Av.”, “Dr.”) identify no one.

How to read a leftover:

- For a person's name or a number, it is a leak: the service knew the value and left one occurrence readable.
- For a place or an organisation, it can be a leak or an over-match. If the service once masked a general word such as “Tarım” as an organisation, every other use of that word counts as left readable, although nothing was exposed.

This metric measures consistency, not recall. A value the service never found is not in the mapping and cannot be counted.

### 4.4 Second opinion from a language model

The two checks above have a blind spot. A name the service never detected has no checksum and is not in the mapping, so neither check can see it. To look into that blind spot, the masked text of every document is given to a language model, which is asked to list the personal data it can still read (`audit.py`). The masked text is what a model would receive in production, so nothing is sent that the product is not meant to send.

Documents are sent in pieces of at most 8,000 characters, cut at line ends. The model is asked for these kinds of value:

| Kind | Set against these types of the service |
|---|---|
| Person name (someone who takes part in the matter) | `PERSON` |
| National ID or passport number | `TR_NATIONAL_ID`, `TR_ID_DOCUMENT` |
| Phone number | `PHONE_NUMBER` |
| E-mail address | `EMAIL_ADDRESS` |
| IBAN, account or card number | `IBAN_CODE`, `BANK_ACCOUNT`, `CREDIT_CARD`, `CARD_CVV` |
| Licence plate | `TR_PLATE` |
| Postal address | `LOCATION`, `POSTAL_CODE` |
| Date of birth | `DATE_OF_BIRTH` |
| Other identifying number | `TR_TAX_ID`, `TR_REGISTRY_NO`, `TR_MERSIS`, `TR_DRIVER_LICENSE`, `TR_UETS`, `TR_ETTN` |
| Company or other private entity | `ORGANIZATION` |
| Author cited as legal literature | listed apart, not counted as a leak |

It is told to leave out courts and other state bodies, statutes, court file numbers, amounts, dates other than dates of birth, and names of cities on their own. Court file numbers are covered by the pattern in section 4.5.

The model's answer is checked before anything is counted:

- A finding counts only if its text occurs in the masked document, outside placeholders, as a whole token. Findings that do not are rejected, and the report says how many.
- Overlapping findings are counted once. “Ahmet Yılmaz” and “Yılmaz” do not count the same word twice.
- Each finding is compared with the mapping. If the service masked the same value, or a longer or shorter form of it, somewhere in the document, the finding is **masked elsewhere**: an inconsistency. Otherwise it is **never detected**: a miss.

The **estimated share masked** for a kind is `masked / (masked + still readable)`, where `masked` is the number of placeholders of the service types in the table. Court file numbers are left out of that count.

This is an estimate, and it can be wrong in both directions:

- The model overlooks values. To measure how many, values the service had masked are unmasked on purpose in a sample of documents, and the share the model reports is counted (`bbmask-bench audit --calibrate`). A leak the service leaves by itself can be harder to see than a planted one, so the model's share on real leaks is probably lower.
- The model reports values that are not personal data, such as a brand or a general word. Company names are the weakest kind: whether a name is a party or a passing mention is a judgement.
- The service types and the model's kinds do not match one to one. `LOCATION`, for example, also holds city names, which the model is told to ignore.

Findings are personal data. They are stored under `runs/` and are not published; the results hold counts only.

### 4.5 Context retention

Masking should leave the legal reasoning readable. Six kinds of expression are counted in the original and in the masked text, after case folding:

| Expression | Example |
|---|---|
| Statute reference | `2918 sayılı` |
| Article reference | `91. maddesi`, `m. 49`, `md. 17/2` |
| High court name | `Yargıtay`, `Danıştay`, `Anayasa Mahkemesi` |
| Decision number | `2019/54321 E.`, `2018/6543 K.` |
| Date | `14.06.2017` |
| Amount | `400.000,00 TL` |

The retained share is the number of expressions still present divided by the number in the original. Masking a decision number or a date is not an error in itself: a case number can identify the parties, and a date of birth is personal data. The table shows what the model can no longer see, so that you can judge it against your use case.

The report also gives the share of each document's characters that were replaced by placeholders.

### 4.6 Latency and reliability

Percentiles use the nearest-rank method. Throughput is the total number of characters divided by the total time spent in `anonymize`. A call is successful if both `anonymize` and `restore` returned without an error.

## 5. How rates are reported

- Rates are computed over occurrences, not over documents. Document counts are given next to them.
- Percentages are rounded down. A rate is shown as 100% only if nothing was left.
- Every number in the READMEs and in `RESULTS.md` is produced by `bbmask-bench report` from `results/full/summary.json`.

## 6. Limits

- **No exact recall figure.** Without labels, personal data the service missed entirely can only be estimated, by the language-model audit. An exact figure needs documents labelled by hand.
- **One language, one domain.** Turkish legal text. Results for other kinds of document may differ.
- **One corpus.** The petitions were drafted with AI and may be more regular than petitions written by hand. The mix of case types and attachments is that of one collection.
- **Default options only.** `llm_review`, `health_data`, `keep_legal_form`, file input and multi-document calls were not measured.
- **One run.** Each document was sent once. Latency depends on the load on the service and on the network at the time.
- **Run by the vendor.** The benchmark is written and run by the team that builds Data Mask. The code, the per-document results and this description are published so that the work can be checked and repeated.

## 7. Reproducing the run

```bash
bbmask-bench run --corpus <corpus> --run-id full --retries 1 --max-bytes 80000 --pause 3
bbmask-bench audit full --model gpt-6-luna
bbmask-bench audit full --model gpt-6-luna --calibrate 40
bbmask-bench report full --publish
```

`run` stores its working files under `runs/full/`. That directory contains the masked text and the mapping of every document, which is personal data, and is ignored by git. `report --publish` writes the files that are safe to share:

| File | Content |
|---|---|
| `results/full/summary.json` | all aggregate numbers |
| `results/full/documents.jsonl` | one row per document: hashed ids, size, timings, counts |
| `results/full/latency.svg`, `entities.svg` | charts |
| `docs/v0.1.0/RESULTS.md`, `RESULTS.tr.md` | the full report |

None of these files contains document text, file names, request ids, account balances or the API key.
