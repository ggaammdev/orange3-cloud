# HIGGS demo: 11 million rows

`examples/higgs-demo.ows` cross-validates a random forest and logistic regression on the whole [HIGGS dataset](https://archive.ics.uci.edu/dataset/280/higgs) on a cloud machine and shows their ROC curves in Orange's own ROC Analysis. HIGGS has 11,000,000 rows and 28 numeric features; the class says whether a simulated particle collision produced a Higgs boson (1, signal) or not (0, background). The file is a 2.6 GB `.csv.gz` (8 GB uncompressed) at the UCI Machine Learning Repository.

On a laptop with 16 GB of memory, Orange can't load this file at all. On a Medium cloud machine the whole workflow takes about 20 minutes and costs about $0.30.

## Run it

1. Open the workflow: `scripts/run-orange.sh examples/higgs-demo.ows`, or File → Open in Orange.
2. In any Cloud widget, click "Cloud settings…", choose your cloud (Google Cloud, AWS or Azure; see the [README](../README.md#choose-a-cloud)) and the size Medium (16 vCPUs, 64 GB) or larger, and click Start.
3. When the machine is connected, Cloud File makes the machine download the file from its URL and load it; nothing goes through your computer. Cloud Test & Score then runs 5-fold cross-validation of both learners on all rows, on the machine.
4. The evaluation results (a prediction for every row and learner, about 0.7 GB) wait for "Download results" in Cloud Test & Score, which asks first. After the download, ROC Analysis draws both curves.
5. Stop the machine in Cloud settings when you're done. Otherwise it stops by itself after the idle timeout.

The file has no header row, so its columns are called `Feature 1` to `Feature 29`, as Orange's File widget names them. `Feature 1` is the class: the workflow sets it as the target in Cloud File's column list. The features are, in order: lepton pT, eta and phi, missing energy magnitude and phi, four jets' pT, eta, phi and b-tag, and the seven high-level features m_jj, m_jjj, m_lv, m_jlv, m_bb, m_wbb and m_wwbb.

## Timings

Laptop: MacBook Pro M1 (8 cores), 16 GB, macOS 26, Orange 3.41.0, with other apps open as usual. The laptop numbers are what Orange's own widgets do: File reads the CSV with Orange's CSV reader, `Feature 1` is set as the target, and Test & Score runs 5-fold stratified cross-validation of Random Forest and Logistic Regression with their default settings (Random Forest: 10 trees). Subsets are the first rows of the file. A run counts as out of memory when it makes the system's swap grow by more than 8 GB.

Cloud: Medium on Google Cloud (`n2-standard-16`, 16 vCPUs, 64 GB) in `us-central1`, at $0.80 an hour, running the workflow above on all rows with the same learners and settings.

| | Rows | Load | Cross-validation | Peak memory | AUC: forest / logistic regression |
|---|---:|---:|---:|---:|---|
| Laptop | 1M | 29 s | 3 min 12 s | 2.8 GB | 0.769 / 0.684 |
| Laptop | 2M | 6 min 25 s (swapping) | 7 min 4 s | 4.2 GB + 1.3 GB swap | 0.776 / 0.684 |
| Laptop | 4M | 13 min 8 s (swapping) | 15 min 47 s | 5.9 GB + 3.7 GB swap | 0.784 / 0.684 |
| Laptop | 11M (all) | **out of memory**: stopped after 8 min | not possible | 5.3 GB + 8 GB swap | – |
| Cloud, Medium | 11M (all) | 3 min 10 s | 15 min 12 s ($0.20) | – | 0.804 / 0.685 |

On the cloud machine, starting it took 60 s, and downloading the evaluation results (704 MB) to the laptop took 69 s over a home connection. The machine ran for about 22 minutes in all, about $0.30. Loading includes downloading the 2.6 GB file from UCI on the machine (between 2 and 3 minutes in our runs).

What this shows:

- **The laptop can't hold the data.** Orange's CSV reader needs about 2.5 GB of memory per million rows of this file, so the whole file would need about 27 GB. Already 2 million rows push a 16 GB Mac into swapping: loading 2M rows takes 13 times as long as 1M, and 4M rows take 13 minutes.
- **More data gives a better model.** The random forest's AUC grows from 0.769 on 1 million rows and 0.784 on 4 million to 0.804 on all 11 million. Logistic regression stays at 0.684, as expected of a linear model with enough data.
- **The cloud machine evaluates all rows in a quarter of an hour, for cents.** That is about as long as the laptop needs for 4 million rows. The laptop, given enough memory, would need about 45 minutes for all rows, on one core.

`TestRealHiggs` in `tests/test_demo.py` runs the workflow this way on Google Cloud, as a user would, and checks that ROC Analysis gets the results of both learners on all 11,000,000 rows:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_GCP_HIGGS=1 .venv/bin/pytest tests/test_demo.py -k test_higgs_on_gcp -s
```
