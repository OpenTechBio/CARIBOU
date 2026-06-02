# Pseudobulk differential gene expression with DESeq2
#
# Variable transfer pattern (run in a ```python``` block BEFORE this):
#   import rpy2.robjects as ro
#   ro.globalenv['sce'] = anndata2ri.py2rpy(adata)
#   # adata.obs must contain: 'cell_type', 'sample_id', 'condition'
#
# After this block, retrieve results in Python:
#   import pandas as pd
#   dge_df = ro.globalenv['dge_results']
#   # convert with anndata2ri or ro.conversion if needed

library(DESeq2)
library(SingleCellExperiment)
library(scuttle)  # for aggregateAcrossCells

cell_types   <- unique(colData(sce)$cell_type)
results_list <- list()

for (ct in cell_types) {
  sub <- sce[, colData(sce)$cell_type == ct]

  # Skip cell types with too few cells or fewer than 3 samples
  n_samples <- length(unique(colData(sub)$sample_id))
  if (ncol(sub) < 10 || n_samples < 3) next

  # Pseudobulk: sum raw counts per sample
  pb <- aggregateAcrossCells(
    sub,
    ids        = colData(sub)$sample_id,
    statistics = "sum",
    use.assay.type = "counts"
  )

  # Build DESeq2 dataset; 'condition' column drives the contrast
  dds <- DESeqDataSet(pb, design = ~condition)
  dds <- dds[rowSums(counts(dds) >= 5) >= 3, ]  # filter low-count genes
  dds <- DESeq(dds, quiet = TRUE)

  # Extract results for the contrast of interest
  res    <- results(dds, contrast = c("condition", "case", "control"))
  res_df <- as.data.frame(res)
  res_df$gene      <- rownames(res_df)
  res_df$cell_type <- ct
  results_list[[ct]] <- res_df
}

dge_results <- do.call(rbind, results_list)
rownames(dge_results) <- NULL

# Top hits
sig <- dge_results[!is.na(dge_results$padj) & dge_results$padj < 0.05, ]
print(head(sig[order(sig$padj), c("gene", "cell_type", "log2FoldChange", "padj")], 20))
