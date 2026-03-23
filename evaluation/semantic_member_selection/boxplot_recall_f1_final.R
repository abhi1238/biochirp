# =============================================================================
# Polished Box Plot: 1 Row x 6 Columns | 190 mm x 45 mm | Arial
# Fix: 3x scale render → scale down in Illustrator for editable live text
# =============================================================================

library(ggplot2)
library(dplyr)
library(tidyr)
library(readxl)
library(patchwork)
library(forcats)
library(svglite)

BASE_FONT <- "Arial"
SCALE     <- 3   # render 3x, scale to 33% in Illustrator

# ── 1. Load & combine datasets ────────────────────────────────────────────────
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

# ── 2. Labels ─────────────────────────────────────────────────────────────────
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

# ── 3. Theme (scaled font sizes) ──────────────────────────────────────────────
theme_polished <- function(sc = SCALE) {
  theme_minimal(base_size = 6 * sc, base_family = BASE_FONT) +
    theme(
      panel.background   = element_rect(fill = "white", colour = NA),
      panel.border       = element_blank(),
      
      axis.line.x.bottom = element_line(colour = "black", linewidth = 0.25 * sc),
      axis.line.y.left   = element_line(colour = "black", linewidth = 0.25 * sc),
      
      panel.grid         = element_blank(),
      
      axis.title.y       = element_text(size = 6 * sc, colour = "black", face = "bold",
                                        margin = margin(r = 2 * sc)),
      axis.title.x       = element_blank(),
      
      axis.text.x        = element_text(size = 5 * sc, colour = "black",
                                        angle = 45, hjust = 1, vjust = 1),
      axis.text.y        = element_text(size = 5 * sc, colour = "black"),
      axis.ticks         = element_line(colour = "black", linewidth = 0.25 * sc),
      axis.ticks.length  = unit(1.5 * sc, "pt"),
      
      plot.title         = element_text(size = 6 * sc, face = "bold", hjust = 0.5,
                                        margin = margin(b = 2 * sc)),
      
      plot.margin        = margin(2 * sc, 3 * sc, 10 * sc, 1 * sc, "pt"),
      legend.position    = "none"
    )
}

# ── 4. Panel Factory ──────────────────────────────────────────────────────────
make_panel <- function(target_metric, target_entity, y_title, p_title) {
  
  plot_df <- df_all %>%
    filter(metric == target_metric, entity_type == target_entity, !is.na(method)) %>%
    mutate(method_clean = updated_names[method]) %>%
    mutate(method_clean = fct_reorder(method_clean, value, .fun = median, .desc = TRUE)) %>%
    mutate(is_bc = grepl("BC", method_clean))
  
  ggplot(plot_df, aes(x = method_clean, y = value)) +
    geom_hline(yintercept = 1.0, linetype = "dashed", colour = "grey85",
               linewidth = 0.3 * SCALE) +
    geom_boxplot(
      aes(fill = is_bc, colour = is_bc),
      alpha         = 0.85,
      linewidth     = 0.25 * SCALE,
      width         = 0.6,
      outlier.shape = 16,
      outlier.size  = 0.3 * SCALE,
      outlier.alpha = 0.5,
      fatten        = 2
    ) +
    scale_fill_manual(values  = c("TRUE" = "#80cbc4", "FALSE" = "grey90")) +
    scale_colour_manual(values = c("TRUE" = "#00796B", "FALSE" = "grey60")) +
    scale_y_continuous(
      name   = y_title,
      limits = c(-0.02, 1.05),
      breaks = c(0, 0.25, 0.50, 0.75, 1.00),
      labels = c("0", "0.25", "0.5", "0.75", "1"),
      expand = c(0, 0)
    ) +
    labs(title = p_title) +
    theme_polished()
}

# ── 5. Build & Compose ────────────────────────────────────────────────────────
p_rec_dis <- make_panel("recall", "Disease", "Recall",   "Disease")
p_rec_drg <- make_panel("recall", "Drug",    "Recall",   "Drug")
p_rec_gen <- make_panel("recall", "Gene",    "Recall",   "Gene")

p_f1_dis  <- make_panel("f1", "Disease", "F1 Score", "Disease")
p_f1_drg  <- make_panel("f1", "Drug",    "F1 Score", "Drug")
p_f1_gen  <- make_panel("f1", "Gene",    "F1 Score", "Gene")

final_plot <- (p_rec_dis | p_rec_drg | p_rec_gen | p_f1_dis | p_f1_drg | p_f1_gen) +
  plot_layout(nrow = 1) +
  plot_annotation(tag_levels = 'A') &
  theme(plot.tag = element_text(size = 7 * SCALE, family = BASE_FONT, face = "bold"))

# ── 6. Export ─────────────────────────────────────────────────────────────────

# PDF (cairo — best for Illustrator live text)
cairo_pdf(
  filename = "boxplot_1row_legend.pdf",
  width    = (190 * SCALE) / 25.4,
  height   = (45  * SCALE) / 25.4,
  onefile  = FALSE
)
print(final_plot)
dev.off()

# SVG (svglite — live text via system font mapping)
svglite::svglite(
  file         = "boxplot_1row_legend.svg",
  width        = (190 * SCALE) / 25.4,
  height       = (45  * SCALE) / 25.4,
  system_fonts = list(sans = "Arial")
)
print(final_plot)
dev.off()

message("Saved PDF and SVG at 3x scale — scale to 33% in Illustrator")