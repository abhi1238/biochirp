# ============================================================
# Final Publication Version: Single Row + Title - X-Title
# ============================================================
plot_metric_row_final <- function(df, output_path) {
  
  if (!require(tidytext)) install.packages("tidytext")
  library(tidytext)
  
  # 1. Update Mapping
  updated_names <- c(
    "BC-FuzzyEq" = "BC-FuzzyEq",
    "BC-Curated" = "BC-Curated",
    "BC-EmbedEq" = "BC-EmbedEq",
    "BC-Final"   = "BC-Final",
    "gpt"        = "GPT-5-Nano",
    "grok"       = "Grok-4.1 (non-reasoning)",
    "gemini"     = "Gemini-2.5-Flash-Lite",
    "llama"      = "LLaMA-3.3-70B-Versatile"
  )
  
  target_metrics <- c("accuracy", "precision", "recall", "specificity")
  
  # 2. Data Preparation
  df_dist <- df %>%
    filter(metric %in% target_metrics) %>%
    mutate(
      method = recode(method, !!!updated_names),
      metric = factor(metric, levels = target_metrics),
      metric_label = toupper(metric)
    ) %>%
    group_by(metric_label, method) %>%
    mutate(med_val = median(value, na.rm = TRUE)) %>%
    ungroup() %>%
    mutate(method_order = reorder_within(method, med_val, metric_label))
  
  # 3. Build Plot
  p <- ggplot(df_dist, aes(x = method_order, y = value, fill = method)) +
    annotate("rect", xmin = -Inf, xmax = Inf, ymin = 0.9, ymax = 1.0, 
             fill = "#F0FDF4", alpha = 0.6) +
    
    geom_violin(alpha = 0.7, color = "white", linewidth = 0.1, scale = "width") +
    geom_boxplot(width = 0.12, color = "#0F172A", outlier.shape = 21, 
                 outlier.size = 0.4, alpha = 0.7, linewidth = 0.3) +
    stat_summary(fun = median, geom = "point", shape = 23, size = 1.2, 
                 fill = "white", stroke = 0.5, color = "#0F172A") +
    
    facet_wrap(~metric_label, nrow = 1, scales = "free_x") +
    
    scale_x_reordered() + 
    scale_y_continuous(limits = c(0, 1.05), breaks = seq(0, 1, 0.25)) +
    
    scale_fill_manual(values = c(
      "BC-Final" = "#1E3A8A", "BC-Curated" = "#3B82F6", "BC-EmbedEq" = "#6495ED", 
      "BC-FuzzyEq" = "#B0C4DE", "GPT-5-Nano" = "#F59E0B", "Gemini-2.5-Flash-Lite" = "#10B981", 
      "LLaMA-3.3-70B-Versatile" = "#8B5CF6", "Grok-4.1 (non-reasoning)" = "#64748B"
    )) +
    
    # ADDED TITLE & REMOVED X-LABEL
    labs(
      title = "Drug Selection Performance Across Architectural Frameworks",
      y = "Score", 
      x = NULL
    ) +
    
    theme_minimal(base_family = "Arial", base_size = 9) +
    theme(
      plot.title = element_text(face = "bold", size = 10, hjust = 0, margin = margin(b=10)),
      strip.background = element_rect(fill = "#1E293B", color = NA),
      strip.text = element_text(face = "bold", color = "white", size = 9),
      
      axis.text.x = element_text(angle = 55, hjust = 1, size = 5.5, color = "#1E293B"),
      axis.ticks.x = element_line(color = "#CBD5E1", linewidth = 0.2),
      
      axis.text.y = element_text(size = 8),
      axis.title.y = element_text(face = "bold", size = 9),
      
      panel.grid.major.x = element_blank(),
      panel.grid.minor = element_blank(),
      panel.spacing = unit(1.0, "lines"),
      
      legend.position = "none",
      plot.margin = margin(10, 5, 5, 5) # Reduced bottom margin
    )
  
  # 4. Save
  ggsave(output_path, plot = p, width = 7.1, height = 2.75, units = "in", dpi = 300, device = cairo_pdf)
  
  message("Success: Final distribution plot saved to ", output_path)
}

# ============================================================
# EXECUTION
# ============================================================
raw_df <- read.csv(
  "~/abhi/biochirp/evaluation/semantic_member_selection/result/drug_complete.csv",
  stringsAsFactors = FALSE
)

plot_metric_row_final(raw_df, "~/abhi/biochirp/evaluation/semantic_member_selection/figure/drug_metric.pdf")