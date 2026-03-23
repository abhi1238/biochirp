# =============================================================================
# Boxplot: Recall & F1 — per method, hued by entity type (Disease/Drug/Gene)
# 2 panels in 1 row | A4 width 190 mm, height 80 mm | Arial 7pt
# No scatter points, no annotation text, arrow only, space between boxes
# =============================================================================

library(ggplot2)
library(dplyr)
library(tidyr)
library(readxl)
library(cowplot)

# ── 0. Font ───────────────────────────────────────────────────────────────────
BASE_FONT <- tryCatch({
  extrafont::loadfonts(device = "pdf", quiet = TRUE)
  "Arial"
}, error = function(e) "sans")

# ── 1. Load & combine all three datasets ─────────────────────────────────────
load_data <- function(path, entity_type) {
  df <- read_excel(path)
  entity_col <- setdiff(names(df), c("method", "metric", "value"))[1]
  df %>%
    rename(entity = all_of(entity_col)) %>%
    mutate(entity_type = entity_type)
}

df_all <- bind_rows(
  load_data("~/abhi/biochirp/evaluation/semantic_member_selection/disease_metric_complete.xlsx", "Disease"),
  load_data("~/abhi/biochirp/evaluation/semantic_member_selection/drug_metric_complete.xlsx",    "Drug"),
  load_data("~/abhi/biochirp/evaluation/semantic_member_selection/gene_metric_complete.xlsx",    "Gene")
)

# ── 2. Method ordering & labels ───────────────────────────────────────────────
method_levels <- c("BC-Final", "BC-FuzzyEq", "BC-Curated", "BC-EmbedEq",
                   "gpt", "grok", "gemini", "llama")

method_labels <- c(
  "BC-Final"   = "BC-Final",
  "BC-FuzzyEq" = "BC-Fuzzy",
  "BC-Curated" = "BC-Curated",
  "BC-EmbedEq" = "BC-Embed",
  "gpt"        = "GPT-4",
  "grok"       = "Grok",
  "gemini"     = "Gemini",
  "llama"      = "LLaMA"
)

# ── 3. Colours ────────────────────────────────────────────────────────────────
entity_colors <- c(
  "Disease" = "#5CA85A",
  "Drug"    = "#D96B52",
  "Gene"    = "#9278BA"
)

entity_fills <- c(
  "Disease" = "#A8DBA5",
  "Drug"    = "#F2B8A8",
  "Gene"    = "#CFC3E0"
)

# ── 4. Prepare plot data ──────────────────────────────────────────────────────
plot_df <- df_all %>%
  filter(metric %in% c("recall", "f1")) %>%
  mutate(
    method      = factor(method, levels = method_levels, labels = method_labels),
    entity_type = factor(entity_type, levels = c("Disease", "Drug", "Gene")),
    metric      = factor(metric, levels = c("recall", "f1"),
                         labels = c("Per-entity recall", "Per-entity F1"))
  ) %>%
  filter(!is.na(method))

# BC-Final overall median per metric (for dotted reference line)
bc_line <- plot_df %>%
  filter(method == "BC-Final") %>%
  group_by(metric) %>%
  summarise(med = median(value, na.rm = TRUE), .groups = "drop")

# ── 5. Theme ──────────────────────────────────────────────────────────────────
theme_box <- function() {
  theme_classic(base_size = 7, base_family = BASE_FONT) +
    theme(
      panel.border       = element_rect(colour = "grey40", fill = NA, linewidth = 0.3),
      panel.grid.major.y = element_line(colour = "grey93", linewidth = 0.25),
      panel.grid.major.x = element_blank(),
      panel.grid.minor   = element_blank(),
      axis.line          = element_blank(),
      
      axis.title.y       = element_text(size = 7, family = BASE_FONT, colour = "grey15"),
      axis.title.x       = element_blank(),
      axis.text.x        = element_text(size = 6, family = BASE_FONT, colour = "grey20",
                                        angle = 35, hjust = 1, vjust = 1),
      axis.text.y        = element_text(size = 6, family = BASE_FONT, colour = "grey30"),
      axis.ticks         = element_line(colour = "grey50", linewidth = 0.25),
      axis.ticks.length  = unit(1.5, "pt"),
      
      legend.position    = "none",
      plot.background    = element_rect(fill = "white", colour = NA),
      plot.margin        = margin(4, 4, 2, 4, "pt")
    )
}

# ── 6. Panel factory ──────────────────────────────────────────────────────────
# dodge.width controls separation between the 3 entity boxes per method group
# box width controls individual box width — narrower = more space between boxes
DODGE_W  <- 0.75
BOX_W    <- 0.22    # narrow boxes with clear gaps between entity groups
SEPARATOR_X <- 4.5  # dashed line between BC-Embed (4) and GPT-4 (5)

make_panel <- function(metric_label, y_title) {
  
  sub    <- plot_df %>% filter(metric == metric_label)
  bc_med <- bc_line  %>% filter(metric == metric_label) %>% pull(med)
  
  ggplot(sub, aes(x = method, y = value)) +
    
    # ── Dotted BC-Final reference line ──────────────────────────────────────
    geom_hline(
      yintercept = bc_med,
      linetype   = "dotted",
      colour     = "grey50",
      linewidth  = 0.4
    ) +
    
    # ── Vertical dashed separator: BC | LLM ─────────────────────────────────
    geom_vline(
      xintercept = SEPARATOR_X,
      linetype   = "dashed",
      colour     = "grey65",
      linewidth  = 0.35
    ) +
    
    # ── Boxplots (dodged, no outlier points) ─────────────────────────────────
    geom_boxplot(
      aes(fill = entity_type, colour = entity_type),
      position      = position_dodge(width = DODGE_W),
      width         = BOX_W,
      outlier.shape = NA,
      linewidth     = 0.35,
      alpha         = 0.78,
      fatten        = 2        # median line weight
    ) +
    
    # ── Arrow only (no text) pointing to BC-Final ────────────────────────────
    annotate(
      "segment",
      x         = 1.45,
      xend      = 1.05,
      y         = bc_med + 0.11,
      yend      = bc_med + 0.02,
      arrow     = arrow(length = unit(3, "pt"), type = "closed"),
      colour    = "#2D6A2D",
      linewidth = 0.55
    ) +
    
    # ── Scales ────────────────────────────────────────────────────────────────
    scale_fill_manual(
      name   = "Entity type",
      values = entity_fills,
      breaks = c("Disease", "Drug", "Gene")
    ) +
    scale_colour_manual(
      name   = "Entity type",
      values = entity_colors,
      breaks = c("Disease", "Drug", "Gene")
    ) +
    scale_y_continuous(
      name   = y_title,
      limits = c(-0.02, 1.10),
      breaks = c(0.00, 0.25, 0.50, 0.75, 1.00),
      labels = c("0.00", "0.25", "0.50", "0.75", "1.00"),
      expand = c(0, 0)
    ) +
    scale_x_discrete(
      labels = method_labels[method_levels]
    ) +
    
    theme_box()
}

# ── 7. Build panels ───────────────────────────────────────────────────────────
p_recall <- make_panel("Per-entity recall", "Per-entity recall")
p_f1     <- make_panel("Per-entity F1",     "Per-entity F1")

# ── 8. Shared legend (horizontal, below) ─────────────────────────────────────
legend_df <- data.frame(
  method      = factor(rep(method_levels[1:3], 1), levels = method_levels),
  entity_type = factor(c("Disease", "Drug", "Gene"),
                       levels = c("Disease", "Drug", "Gene")),
  value       = c(0.5, 0.5, 0.5)
)

legend_plot <- ggplot(legend_df,
                      aes(x = method, y = value, fill = entity_type, colour = entity_type)) +
  geom_boxplot(width = 0.4, linewidth = 0.3, alpha = 0.78) +
  scale_fill_manual(name = "Entity type", values = entity_fills,
                    breaks = c("Disease", "Drug", "Gene")) +
  scale_colour_manual(name = "Entity type", values = entity_colors,
                      breaks = c("Disease", "Drug", "Gene")) +
  theme_void(base_family = BASE_FONT) +
  theme(
    legend.position  = "bottom",
    legend.direction = "horizontal",
    legend.title     = element_text(size = 7, family = BASE_FONT,
                                    face = "bold", colour = "grey15"),
    legend.text      = element_text(size = 6, family = BASE_FONT, colour = "grey20"),
    legend.key.size  = unit(7, "pt"),
    legend.key       = element_blank(),
    legend.spacing.x = unit(3, "pt"),
    legend.margin    = margin(0),
    plot.background  = element_rect(fill = "white", colour = NA)
  ) +
  guides(
    fill   = guide_legend(nrow = 1, override.aes = list(alpha = 0.80, size = 0.3)),
    colour = "none"
  )

shared_legend <- get_legend(legend_plot)

# ── 9. Compose ────────────────────────────────────────────────────────────────
panels_row <- plot_grid(
  p_recall, p_f1,
  ncol             = 2,
  align            = "hv",
  axis             = "tblr",
  labels           = c("A", "B"),
  label_size       = 7,
  label_fontfamily = BASE_FONT,
  label_fontface   = "bold"
)

final_plot <- plot_grid(
  panels_row,
  shared_legend,
  ncol        = 1,
  rel_heights = c(1, 0.11)
)

# ── 10. Export ────────────────────────────────────────────────────────────────
ggsave(
  filename = "boxplot_recall_f1.pdf",
  plot     = final_plot,
  width    = 190, height = 80, units = "mm",
  device   = cairo_pdf
)

ggsave(
  filename = "boxplot_recall_f1.png",
  plot     = final_plot,
  width    = 190, height = 80, units = "mm",
  dpi      = 300
)

message("Saved: boxplot_recall_f1.pdf and boxplot_recall_f1.png")