# Orange's widgets on cloud data

A cloud table (the output of Cloud File) is an Orange table with the cloud table's columns and **one placeholder row in which every value is missing**. The data itself stays on the cloud machine. Orange's own widgets therefore see no values:

- Widgets that **change the data** (choose columns, filter rows, compute new columns) record their steps, and the machine replays them on all rows when a Cloud widget needs the data.
- Widgets that **look at or compute from the data** see only the empty row. Use the Cloud widget for the job instead, which computes on all rows on the machine.
- Widgets whose output is plain local data (a sample, merged or reshaped rows) give tables that the Cloud widgets refuse with "This is local data, not a cloud table."
- Widgets that compute **distributions or contingencies** of a cloud table (counts of values, also per class) stop with an error, which Orange shows in its error report: "Orange's widgets can't compute statistics of a cloud table on this computer: …". It names the Cloud widget to use. Rank, Discretize and Purge Domain stop this way in their most common settings, and so do Mosaic Display, Sieve Diagram and Correspondence Analysis. Widgets that read the empty row's values directly can't be stopped; their entries below say what they do. Rank, Discretize, Purge Domain and Create Class have Cloud widgets with the same options that compute on all rows: Cloud Rank, Cloud Discretize, Cloud Purge Domain and Cloud Create Class. For Edit Domain's "Merge infrequent" and "As categorical", use Cloud Categories.

Checked with Orange 3.41 (the errors above also with Orange 3.39) on a 3,000-row cloud table, by comparing each replayed result with the same widget's output on the full table. `tests/test_supported_widgets.py` repeats the checks (`pytest -m slow` for all of them).

## Steps replayed on all rows

- **Select Columns**: as in Orange.
- **Select Rows**: as in Orange. Matching and Unmatched Data both replay. Annotated Data is local data.
- **Feature Constructor**: as in Orange. Give categorical features their list of values; without one, the machine refuses the step.
- **Edit Domain**: renaming columns, renaming and merging categories by hand, and turning categorical columns into numeric or text columns replay. Its "Merge infrequent" and "As categorical" need the values of all rows: use Cloud Categories for them.
  - **Merge infrequent** merges nothing on a cloud table, in each of its modes (fewer than N, less than p %, all but the N most frequent): it counts values on the empty row, which has none. The output is the unchanged table, without a message. Cloud Categories has the same modes and counts on all rows.
  - **As categorical** for numeric, text and time columns takes its categories from the empty row and is refused. Cloud Categories makes numeric and text columns categorical with the values of all rows, in the order Edit Domain gives them (time columns aren't supported).
  - Most other type changes, such as text to numeric, are refused as not supported.
- **Continuize**: one-hot and other codings of categorical columns replay. Normalizing numeric columns learns from the data and is refused: use Cloud Preprocess.
- **Color**: as in Orange.

## Refused: use Cloud Preprocess

These steps learn their settings (means, cut points, components) from the data. Build the recipe in Orange's Preprocess widget and connect its Preprocessor output to Cloud Preprocess, which fits it on all rows.

- **Preprocess**: build the recipe without data connected to Preprocess, and connect its Preprocessor output to Cloud Preprocess. With a cloud table connected, its Preprocessed Data is refused, and a step that counts values (Discretize, Impute, Select Relevant Features with a counting score) stops Preprocess with the error above. It then doesn't send the changed recipe either, so Cloud Preprocess would keep the previous one. The add-on can't change this: Orange's Preprocess sends its recipe only after applying it to its data, and letting that through would let Rank, Discretize and Purge Domain choose columns from the empty row without an error.
- **Impute**: gives local data. Use Impute in Cloud Preprocess.

## Show nothing on cloud data: use a Cloud widget

- **Data Table**: shows the empty placeholder row. Use Cloud Data Table.
- **Data Info**: shows the real row count in the name, but "1 row" as the size. Use Cloud Data Table.
- **Feature Statistics**: all values missing. Use Cloud Feature Statistics.
- **Distributions**: "does not have any defined values". Use Cloud Distributions.
- **Box Plot**: empty. Use Cloud Box Plot.
- **Scatter Plot**: "missing for all data points". Use Cloud Scatter Plot.
- **Logistic Regression, Tree, Random Forest, kNN, Naive Bayes, SVM, Gradient Boosting, Neural Network, Constant**: with cloud data as input they show "Data contains a single target value." Don't connect the data: connect their Learner output to Cloud Learner or Cloud Test & Score.
- **Test and Score**: "Target variable has no values." Use Cloud Test & Score.
- **Predictions**: predicts the empty row. Use Cloud Predictions.
- **k-Means**: "Too few (1) unique data instances". Use Cloud k-Means.
- **t-SNE**: "Input data needs at least 2 rows". Use Cloud t-SNE, and view its output with Cloud Scatter Plot.
- **Distances**: gives the 1 × 1 matrix of the empty row, without a message. Use Cloud Distances: its matrix stays on the machine for the Cloud widgets that take distances; Orange's own widgets can't take it.
- **Hierarchical Clustering**: with the matrix of Orange's Distances on cloud data: "Distance matrix is empty." Use Cloud Hierarchical Clustering on Cloud Distances' output.
- **MDS**: "Input data needs at least 2 rows". Use Cloud MDS on the cloud table or on Cloud Distances' output, and view its output with Cloud Scatter Plot.

## Use the Cloud widgets' outputs

These work as usual on Cloud Test & Score's Evaluation Results, computed on all rows.

- **Confusion Matrix**: as in Orange. Its Selected Data and Data outputs hold only the target column.
- **ROC Analysis, Lift Curve, Calibration Plot**: as in Orange.

## Not supported

- **Rank**: use Cloud Rank, which scores on all rows. With Information Gain, Gain Ratio, Gini or FCBF checked (Gain Ratio and Gini are its defaults for a categorical target), Rank stops with the error above. It then stays busy until its data or its scoring methods change. The add-on can't end that: Orange's Rank passes errors from its scoring on to Orange's error report without finishing, and it turns a `ValueError` into empty scores, after which it would choose arbitrary columns without a message. ANOVA, χ², ReliefF, and for a numeric target Univariate Regression and RReliefF (its defaults), and learners on its Scorer input can't be stopped: they give no scores on the empty row, so Best Ranked picks arbitrary columns.
- **Discretize**: use Cloud Discretize, which has Discretize's methods and settings per column and finds the cut points on all rows (or Discretize in Cloud Preprocess). With Equal frequency or Entropy vs. MDL, Discretize stops with the error above. Custom cut points are refused by the machine, with a message that names Cloud Discretize. Equal width, Natural binning and Fixed width can't be stopped: the empty row has nothing to cut, so they drop the numeric columns.
- **Purge Domain**: use Cloud Purge Domain, which has Purge Domain's options and decides on all rows. On a table with numeric columns, its default "Remove constant features" stops with the error above. "Remove unused values" without it is refused by the machine, because the empty row uses none of the values; the message names Cloud Purge Domain. On a table without numeric columns it can't be stopped: it removes every categorical column.
- **PCA**: shows an error report. Use PCA in Cloud Preprocess.
- **Create Class**: refused by the machine. It counts matches on the empty row, so on a cloud table it keeps only its first rule. Use Cloud Create Class, which has Create Class's options and keeps its rules as Create Class does on all rows.
- **Aggregate Columns**: gives local data. Use Feature Constructor, e.g. `x1 + x2`.
- **Randomize, Data Sampler, Unique, Merge Data, Concatenate, Select by Data Index, Transpose, Group By, Pivot Table, Melt, Create Instance**: give local data, or nothing.
- **Correlations, Outliers, Neighbors**: compute on the empty row and show an error.
- **Save Data**: saves only the empty placeholder row.
- **Violin Plot, Line Plot, Bar Plot, Heat Map, Mosaic Display, Sieve Diagram, Linear Projection, Venn Diagram, Silhouette Plot**: show nothing or an error.
- **Louvain Clustering, DBSCAN, Manifold Learning, Correspondence Analysis**: compute on the empty row: an error, or nothing useful.
