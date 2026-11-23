# Changelog

The changes in each release of orange3-cloud.

## Unreleased

- The Cloud settings dialog's "Notify me about the hosted version" button works: it opens the [roadmap issue for a hosted service](https://github.com/ggaammdev/orange3-cloud/issues/13), where you can upvote it and subscribe to its updates.

## 1.0.0

The first release. Build workflows in Orange on your own computer as usual, and run the heavy computation on a machine in your own cloud account.

- **Three clouds:** Google Cloud, AWS and Azure, in your own project, account or subscription, signed in with the cloud's own command-line tool. Cloud settings has a page for each, with a setup check that says what is still missing and how to fix it. The same sizes on every cloud, from Small (4 vCPUs, 16 GB) up to at least 96 vCPUs and 768 GB, with each size's hourly price in your region.
- **Local mode:** the same machine runs in Docker on your computer, at no cost, for trying the add-on and for small data.
- **Your exact environment:** the machine installs your versions of Python, Orange and your add-ons when it starts, so learners, models and results travel between Orange and the machine unchanged.
- **Data stays in the cloud:** Cloud File loads CSV, Orange's `.tab` and Parquet files (also gzipped, also several with the same columns) into the machine's memory from your computer, a URL, or your cloud's storage (`gs://`, `s3://`, `az://`). Orange on your computer holds only a reference, and every result comes from all rows, never from a sample.
- **Cloud widgets:** Cloud File, Cloud Data Table, Cloud Feature Statistics, Cloud Distributions, Cloud Box Plot, Cloud Scatter Plot, Cloud Preprocess, Cloud Rank, Cloud Discretize, Cloud Purge Domain, Cloud Categories, Cloud Create Class, Cloud Learner, Cloud Test & Score (folds and learners in parallel), Cloud Predictions, Cloud k-Means, Cloud t-SNE, Cloud MDS, Cloud Distances and Cloud Hierarchical Clustering, each with a help page.
- **Orange's own widgets on cloud data:** Select Columns, Select Rows, Feature Constructor, Edit Domain, Continuize and Color record their steps, and the machine replays them on all rows. Learners come from Orange's learner widgets, and results go to Orange's ROC Analysis, Confusion Matrix and others. `docs/supported-widgets.md` lists which of Orange's widgets work on cloud data.
- **Demos:** a year of New York taxi trips (48.7 million rows; a one-month version for Small machines) and the 11-million-row HIGGS dataset, with workflows in `examples/` and measured timings and costs.
- **Costs in view:** each Cloud widget shows what its last job cost, Cloud settings shows the machine's cost so far, and the add-on keeps a record of every machine's cost on your computer. Machines stop by themselves after 30 minutes without work and after at most 12 hours, and cancel jobs that Orange no longer waits for. Stop deletes the machine with its disk and network rule.
- **Security:** each machine has its own random token and its own HTTPS certificate, which the add-on trusts exclusively; the network rule lets only your computer's IP address reach the machine; the storage for your files is private. Workflow files contain no credentials or connection settings, and the add-on sends no usage statistics.
- **Requirements:** Orange 3 on Python 3.10 or newer. Tested on macOS on Apple Silicon; Windows and Linux should work but aren't tested.
