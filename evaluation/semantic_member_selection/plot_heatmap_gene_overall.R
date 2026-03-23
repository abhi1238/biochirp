# ============================================================
# Libraries
# ============================================================
library(ggplot2)
library(tidyr)
library(dplyr)
library(forcats)
library(grid)
library(ggtext)
library(scales)
library(ggnewscale)

# ============================================================
# Global Palettes
# ============================================================
soft_rdylgn <- c(
  "#D73027", "#F46D43", "#FDAE61", "#FEE08B",
  "#FFFFBF", "#D9EF8B", "#A6D96A", "#66BD63", "#1A9850"
)

metric_colors <- c(
  "f1" = "#F43F5E",
  "accuracy" = "#3B82F6",
  "precision" = "#10B981",
  "recall" = "#F59E0B",
  "kappa" = "#8B5CF6",
  "specificity" = "#64748B"
)

# ============================================================
# Method Dictionaries
# ============================================================
method_header_map <- c(
  "BC-FuzzyEq"  = "BC-FuzzyEq",
  "BC-Curated" = "BC-Curated",
  "BC-EmbedEq" = "BC-EmbedEq",
  "BC-Final"   = "BC-Final",
  "gpt"        = "gpt-4o-mini",
  "grok"       = "grok-4.1-fast-non-reasoning",
  "gemini"     = "gemini-2.5-flash-lite",
  "llama"      = "llama-3.3-70b-versatile"
)

method_class_map <- c(
  "BC-FuzzyEq"  = "BioChirp",
  "BC-Curated" = "BioChirp",
  "BC-EmbedEq" = "BioChirp",
  "BC-Final"   = "BioChirp",
  "gpt"        = "LLM",
  "grok"       = "LLM",
  "gemini"     = "LLM",
  "llama"      = "LLM"
)

# ============================================================
# Helper: compute method statistics + HTML label
# ============================================================
compute_method_stats <- function(df) {
  df %>%
    filter(metric %in% c("f1", "recall", "precision", "kappa")) %>%
    group_by(method, metric) %>%
    summarise(med = median(value, na.rm = TRUE), .groups = "drop") %>%
    pivot_wider(names_from = metric, values_from = med) %>%
    mutate(method_class = recode(method, !!!method_class_map)) %>%
    arrange(desc(f1)) %>%
    mutate(
      rank = row_number(),
      method_label = paste0(
        "<span style='display:block;width:100%;padding:6px;color:white;text-align:center;'>",
        "<span style='font-size:8pt;font-weight:700;'>",
        recode(method, !!!method_header_map),
        "</span><br>",
        "<span style='font-size:7pt;'>",
        "Rank ", rank,
        " | Med F1: ", sprintf("%.2f", f1),
        " | R: ", sprintf("%.2f", recall),
        " | P: ", sprintf("%.2f", precision),
        " | K: ", sprintf("%.2f", kappa),
        "</span></span>"
      )
    )
}

# ============================================================
# Function 1: A4 Heatmap (FIXED HEADERS)
# ============================================================
plot_performance_heatmap <- function(df, output_path) {
  
  method_stats <- compute_method_stats(df)
  
  df_final <- df %>%
    mutate(
      metric = factor(metric, levels = c(
        "precision", "recall", "f1",
        "accuracy", "specificity", "kappa"
      )),
      gene = factor(gene)
    ) %>%
    left_join(method_stats %>% select(method, method_label, f1), by = "method") %>%
    mutate(method_label = fct_reorder(method_label, f1)) %>%
    group_by(gene) %>%
    mutate(gene_performance = median(value[metric == "f1"], na.rm = TRUE)) %>%
    ungroup() %>%
    mutate(gene = fct_reorder(gene, gene_performance))
  
  p <- ggplot(df_final, aes(x = metric, y = gene, fill = value)) +
    geom_tile(color = "white", linewidth = 0.1) +
    geom_text(
      aes(label = sprintf("%.2f", value),
          color = value > median(value, na.rm = TRUE)),
      size = 2.1, show.legend = FALSE
    ) +
    scale_color_manual(values = c("TRUE" = "white", "FALSE" = "black")) +
    scale_x_discrete(labels = toupper) +
    scale_y_discrete(labels = toupper) +
    facet_wrap(~ method_label, ncol = 2, scales = "free_x") +
    scale_fill_gradientn(
      colors = soft_rdylgn,
      values = rescale(c(0, 0.4, 0.6, 0.8, 1)),
      limits = c(0, 1),
      na.value = "grey90",
      guide = guide_colorbar(
        title = "Performance Score",
        title.position = "top",
        title.hjust = 0.5,
        barheight = unit(0.25, "cm"),
        barwidth = unit(5, "cm")
      )
    ) +
    labs(
      title = "Gene Synonyms Extraction: Comparative Performance Matrix",
      subtitle = "Methods ranked by Median F1",
      x = NULL, y = "Gene"
    ) +
    theme_minimal(base_family = "Arial") +
    theme(
      strip.background = element_rect(fill = "#1e293b", color = NA),
      strip.text = element_markdown(
        color = "white", face = "bold", size = 7,
        margin = margin(6, 6, 6, 6)
      ),
      axis.text.x = element_text(angle = 45, hjust = 1, size = 8),
      axis.text.y = element_text(size = 8),
      panel.spacing = unit(0.45, "lines"),
      panel.grid = element_blank(),
      legend.position = "bottom",
      plot.margin = margin(12, 12, 12, 12)
    )
  
  ggsave(output_path, p,
         width = 8.27, height = 11.69,
         units = "in", dpi = 300, device = cairo_pdf)
}

# ============================================================
# Function 2: Fingerprint PDF (COMPACT)
# ============================================================
plot_performance_fingerprint <- function(df, output_path) {
  
  df_summary <- df %>%
    group_by(method, metric) %>%
    summarise(median_val = median(value, na.rm = TRUE), .groups = "drop")
  
  method_range <- df_summary %>%
    group_by(method) %>%
    summarise(
      min_val = min(median_val),
      max_val = max(median_val),
      f1_val  = median_val[metric == "f1"]
    )
  
  df_summary <- df_summary %>%
    left_join(method_range, by = "method") %>%
    mutate(
      method = fct_reorder(method, f1_val),
      metric = factor(
        metric,
        levels = c("accuracy", "f1", "precision", "recall", "kappa", "specificity")
      ),
      is_bc = grepl("BC-", method)
    )
  
  metric_colors <- c(
    "f1" = "#F43F5E",
    "accuracy" = "#3B82F6",
    "precision" = "#10B981",
    "recall" = "#F59E0B",
    "kappa" = "#8B5CF6",
    "specificity" = "#64748B"
  )
  
  y_label_colors <- ifelse(
    levels(df_summary$method) %in% df_summary$method[df_summary$is_bc],
    "#3730A3", "#1E293B"
  )
  
  p2 <- ggplot(df_summary) +
    
    annotate(
      "rect",
      xmin = 0.8, xmax = 1.0,
      ymin = -Inf, ymax = Inf,
      fill = "#EEF2FF", alpha = 0.5
    ) +
    
    geom_rect(
      data = distinct(df_summary, method),
      aes(
        ymin = as.numeric(method) - 0.45,
        ymax = as.numeric(method) + 0.45,
        xmin = -Inf, xmax = Inf
      ),
      fill = rep(
        c("transparent", "#F8FAFC"),
        length.out = n_distinct(df_summary$method)
      ),
      inherit.aes = FALSE
    ) +
    
    geom_segment(
      aes(
        x = min_val, xend = max_val,
        y = method, yend = method,
        color = is_bc
      ),
      linewidth = 1.1,
      show.legend = FALSE
    ) +
    
    scale_color_manual(values = c("TRUE" = "#C7D2FE", "FALSE" = "#E2E8F0")) +
    new_scale_color() +
    
    geom_point(
      aes(x = median_val, y = method, color = metric),
      size = 1.2, alpha = 0.9, shape = 16
    ) +
    
    geom_point(
      data = filter(df_summary, metric == "f1"),
      aes(x = median_val, y = method, color = metric),
      fill = "#F43F5E",
      size = 2.1,
      shape = 23,
      stroke = 0.5,
      color = "white"
    ) +
    
    geom_text(
      data = filter(df_summary, metric == "f1"),
      aes(x = median_val, y = method, label = sprintf("%.2f", median_val)),
      nudge_y = 0.4,
      size = 1.9,
      fontface = "bold",
      color = "#1E1B4B"
    ) +
    
    scale_color_manual(values = metric_colors) +
    
    scale_x_continuous(
      limits = c(0, 1.1),
      breaks = seq(0, 1, 0.25),
      expand = c(0, 0)
    ) +
    
    coord_cartesian(clip = "off") +
    
    labs(
      title = "Gene metric comparison",
      x = "Median Metric Score",
      y = NULL
    ) +
    
    theme_minimal(base_size = 7) +
    
    theme(
      axis.line = element_line(color = "#1E293B", linewidth = 0.5),
      panel.grid = element_blank(),
      
      axis.text.y = element_text(
        face = "bold",
        size = 6.5,
        color = y_label_colors
      ),
      
      axis.text.x = element_text(
        size = 6,
        color = "#475569"
      ),
      
      axis.title.x = element_text(
        size = 6,
        face = "bold",
        color = "#1E293B",
        margin = margin(t = 6)
      ),
      
      # ? LEGEND ? EXACTLY AS ORIGINAL
      legend.position = "bottom",
      legend.justification = "left",
      legend.title = element_blank(),
      legend.text = element_text(size = 5.5, color = "#1E293B"),
      legend.key.size = unit(0.2, "cm"),
      legend.spacing.y = unit(-0.15, "cm"),
      legend.margin = margin(t = -2, l = -12),
      
      plot.title = element_text(
        face = "bold",
        size = 8,
        color = "#1E1B4B"
      ),
      
      plot.margin = margin(10, 15, 5, 5)
    ) +
    
    guides(
      color = guide_legend(
        ncol = 3,
        override.aes = list(
          shape = c(16, 23, 16, 16, 16, 16),
          fill  = c(NA, "#F43F5E", NA, NA, NA, NA),
          color = c(
            "#3B82F6", "white",
            "#10B981", "#F59E0B",
            "#8B5CF6", "#64748B"
          ),
          size = c(1.2, 2.1, 1.2, 1.2, 1.2, 1.2)
        )
      )
    )
  
  ggsave(
    output_path,
    plot = p2,
    width = 2.2,
    height = 2.2,
    units = "in",
    dpi = 300,
    device = cairo_pdf
  )
}

plot_metric_distribution <- function(df, output_path) {
  
  df_dist <- df %>%
    filter(metric %in% c("f1", "accuracy", "precision", "recall", "kappa", "specificity")) %>%
    mutate(
      metric = factor(
        metric,
        levels = c("f1", "accuracy", "precision", "recall", "kappa", "specificity")
      ),
      metric_label = toupper(metric)
    ) %>%
    group_by(metric_label, method) %>%
    mutate(
      med = median(value, na.rm = TRUE),
      q1  = quantile(value, 0.25, na.rm = TRUE),
      q3  = quantile(value, 0.75, na.rm = TRUE)
    ) %>%
    ungroup() %>%
    mutate(
      method_facet = fct_reorder(
        interaction(method, metric_label, sep = "___"),
        med
      )
    )
  
  p <- ggplot(df_dist, aes(x = value, y = method_facet)) +
    
    # Success zone
    annotate(
      "rect",
      xmin = 0.8, xmax = 1.05,
      ymin = -Inf, ymax = Inf,
      fill = "#F1F5F9",
      alpha = 0.6
    ) +
    
    # Slim violin for distribution
    geom_violin(
      aes(fill = metric),
      width = 0.8,
      alpha = 0.25,
      color = NA,
      scale = "width"
    ) +
    
    # IQR range
    geom_linerange(
      aes(xmin = q1, xmax = q3),
      linewidth = 0.7,
      color = "#1E293B"
    ) +
    
    # Median point
    geom_point(
      aes(x = med),
      size = 1.2,
      shape = 21,
      fill = "white",
      stroke = 0.6,
      color = "#1E293B"
    ) +
    
    facet_wrap(~metric_label, ncol = 3, scales = "free_y") +
    
    scale_y_discrete(labels = function(x) sub("___.*", "", x)) +
    
    scale_fill_manual(values = metric_colors) +
    
    scale_x_continuous(
      limits = c(0, 1.05),
      breaks = seq(0, 1, 0.25)
    ) +
    
    labs(
      title = "Metric Distribution Across Methods for gene",
      subtitle = "Violin = distribution, point = median, bar = IQR (25-75%)",
      x = "Score",
      y = NULL
    ) +
    
    theme_minimal(base_size = 7) +
    theme(
      plot.title = element_text(face = "bold", size = 9, color = "#0F172A"),
      plot.subtitle = element_text(size = 6.5, color = "#64748B"),
      
      strip.background = element_rect(fill = "#1E293B", color = NA),
      strip.text = element_text(face = "bold", color = "white", size = 6),
      
      axis.text.y = element_text(size = 5.5, color = "#1E293B"),
      axis.text.x = element_text(size = 5),
      
      panel.grid.major.y = element_blank(),
      panel.grid.minor = element_blank(),
      panel.spacing = unit(0.6, "lines"),
      
      legend.position = "none",
      plot.margin = margin(6, 8, 6, 6)
    )
  
  ggsave(
    output_path,
    plot = p,
    width = 7,
    height = 4.6,
    units = "in",
    dpi = 300,
    device = cairo_pdf
  )
}

# ============================================================
# EXECUTION (BOTH PDFs)
# ============================================================
raw_df <- read.csv(
  "~/abhi/biochirp/evaluation/semantic_member_selection/result/gene_complete.csv",
  stringsAsFactors = FALSE
)

plot_performance_heatmap(
  raw_df,
  "~/abhi/biochirp/evaluation/semantic_member_selection/figure/gene_synonyms_final_heatmap.pdf"
)

plot_performance_fingerprint(
  raw_df,
  "~/abhi/biochirp/evaluation/semantic_member_selection/figure/gene_metric.pdf.pdf"
)



# 3. The Distribution Analysis (New)
plot_metric_distribution(raw_df, "~/abhi/biochirp/evaluation/semantic_member_selection/figure/gene_distribution_analysis.pdf")
