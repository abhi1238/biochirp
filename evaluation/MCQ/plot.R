library(readxl)
library(lattice)

# 1. Load data
df <- as.data.frame(read_excel("~/abhi/biochirp/evaluation/MCQ/Total_result_mcq.xlsx"))

# 2. Aggregate means using Base R
acc_cols <- grep("is_correct_run_", names(df), value = TRUE)
time_cols <- grep("time_taken_run_", names(df), value = TRUE)

acc_means <- aggregate(df[acc_cols], list(model = df$model), mean, na.rm = TRUE)
time_means <- aggregate(df[time_cols], list(model = df$model), mean, na.rm = TRUE)

# 3. Create initial matrices
acc_mat  <- as.matrix(acc_means[,-1])
rownames(acc_mat) <- acc_means$model
time_mat <- as.matrix(time_means[,-1])
rownames(time_mat) <- time_means$model

# --- SORTING LOGIC ---
# Calculate median accuracy per model (row-wise)
# We use apply(..., 1, median) to get the median of each model across runs
row_medians <- apply(acc_mat, 1, median)

# Get the sorting index (low to high)
sort_idx <- order(row_medians)

# Reorder both matrices using the same index
acc_mat  <- acc_mat[sort_idx, ]
time_mat <- time_mat[sort_idx, ]
# ---------------------

# 4. Cleaning Column Names
colnames(acc_mat)  <- gsub("is_correct_run_", "Run ", colnames(acc_mat))
colnames(time_mat) <- gsub("time_taken_run_", "Run ", colnames(time_mat))

# 5. Plotting Function (unchanged)
render_heatmap <- function(mat, title, color_ramp, label_suffix = "") {
  levelplot(t(mat), 
            aspect = "iso", 
            xlab = "", ylab = "", 
            main = list(label = title, cex = 1.1),
            scales = list(x = list(rot = 45)),
            col.regions = color_ramp,
            panel = function(...) {
              panel.levelplot(...)
              panel.text(..., labels = sprintf(paste0("%.2f", label_suffix), list(...)$z), 
                         cex = 0.8, font = 2)
            })
}

# 6. Save
acc_colors <- colorRampPalette(c("#f0f9e8", "#7bccc4", "#0868ac"))(100)
time_colors <- colorRampPalette(c("#fff7ec", "#fc8d59", "#7f0000"))(100)


pdf("sorted_model_benchmark.pdf", width = 12, height = 6)
print(render_heatmap(acc_mat, "Model Accuracy (Sorted by Median)", acc_colors), 
      split = c(1, 1, 2, 1), more = TRUE)
print(render_heatmap(time_mat, "Latency (Follows Accuracy Order)", time_colors, "s"), 
      split = c(2, 1, 2, 1))
dev.off()