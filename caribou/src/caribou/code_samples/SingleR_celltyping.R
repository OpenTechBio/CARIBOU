# Reference-based cell type annotation with SingleR
#
# Variable transfer pattern (run in a ```python``` block BEFORE this):
#   import rpy2.robjects as ro
#   ro.globalenv['sce'] = anndata2ri.py2rpy(adata)
#
# After this block, retrieve results in Python:
#   singler_labels = list(ro.globalenv['singler_labels'])
#   adata.obs['cell_type_singler'] = singler_labels

library(SingleR)
library(celldex)
library(BiocParallel)

# Choose a reference — select the closest match to the tissue type:
#   celldex::HumanPrimaryCellAtlasData()  — broad human cell types
#   celldex::BlueprintEncodeData()        — immune + stromal (bulk RNA-seq reference)
#   celldex::MonacoImmuneData()           — immune cell subtypes
#   celldex::DatabaseImmuneCellExpressionData() — immune, fine-grained
ref <- celldex::HumanPrimaryCellAtlasData()

# Run SingleR on the SingleCellExperiment (sce) passed from Python
pred <- SingleR(
  test   = sce,
  ref    = ref,
  labels = ref$label.main,
  BPPARAM = MulticoreParam(4)
)

# Diagnostic: label distribution and per-cell confidence
print(table(pred$labels))
print(summary(pred$scores))

# Store for retrieval from Python
singler_labels <- pred$labels
