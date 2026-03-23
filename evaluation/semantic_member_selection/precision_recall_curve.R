# 1. Setup Environment
if (!require("pacman")) install.packages("pacman")
pacman::p_load(tidyverse, patchwork, ggrepel)

base_font <- "sans" # Native vector mapping

# 2. Load and Prepare Data
df_raw <- read_csv("~/abhi/biochirp/evaluation/semantic_member_selection/metrics_export.csv", show_col_types = FALSE)

method_levels <- c('BC-FuzzyEq', 'BC-EmbedEq', 'BC-Curated', 'BC-Final', 'gpt', 'grok', 'gemini', 'llama')
method_labels <- c('BC-Fuzzy', 'BC-Embed', 'BC-Curated', 'BC-Final', 'GPT-4o-mini', 'Grok-4.1', 'Gemini-2.5', 'LLaMA-3.3')

df_raw <- df_raw %>%
  mutate(
    method = factor(method, levels = method_levels, labels = method_labels),
    Domain = factor(Domain, levels = c("Drug", "Gene"))
  )

df_mean <- df_raw %>%
  group_by(Domain, method) %>%
  summarize(
    precision = mean(precision, na.rm = TRUE),
    recall = mean(recall, na.rm = TRUE),
    f1 = mean(f1, na.rm = TRUE),
    .groups = "drop"
  )

# 3. Premium High-Contrast Palette
# BC-Final is a bold crimson to stand out. Baselines are cooler/muted tones.
hex_colors <- c(
  'BC-Fuzzy' = '#3498DB',     # Bright Blue
  'BC-Embed' = '#2ECC71',     # Emerald Green
  'BC-Curated' = '#E67E22',   # Carrot Orange
  'BC-Final' = '#E74C3C',     # Bold Crimson (Focal Point)
  'GPT-4o-mini' = '#9B59B6',  # Amethyst Purple
  'Grok-4.1' = '#34495E',     # Slate Grey
  'Gemini-2.5' = '#D4AC0D',   # Muted Gold
  'LLaMA-3.3' = '#1ABC9C'     # Turquoise
)

shapes_map <- c(
  'BC-Fuzzy' = 16, 'BC-Embed' = 15, 'BC-Curated' = 17, 'BC-Final' = 8,  
  'GPT-4o-mini' = 18, 'Grok-4.1' = 9, 'Gemini-2.5' = 3, 'LLaMA-3.3' = 4      
)

# 4. Generate F1 Iso-curves
f1_levels <- c(0.2, 0.4, 0.6, 0.8)
f1_curves <- expand_grid(recall = seq(0.01, 1.1, length.out = 200), f1 = f1_levels) %>%
  mutate(precision = (f1 * recall) / (2 * recall - f1)) %>%
  filter(precision >= 0 & precision <= 1.15)

f1_labels <- tibble(f1 = f1_levels, recall = f1_levels, precision = f1_levels)

# 5. Build the Plot
p <- ggplot() +
  # A. Iso-curves
  geom_line(data = f1_curves, aes(x = recall, y = precision, group = f1), 
            color = "#D0D0D0", linetype = "dashed", linewidth = 0.5) +
  geom_text(data = f1_labels, aes(x = recall + 0.025, y = precision + 0.025, label = paste0("F1=", f1)), 
            color = "#999999", size = 2.5, family = base_font, fontface = "italic") +
  
  # B. Faint Background Dots
  geom_jitter(data = df_raw, aes(x = recall, y = precision, color = method, shape = method), 
              alpha = 0.25, size = 1.5, width = 0.008, height = 0.008) +
  
  # C. NEW: Confidence Ellipses (Creates visual grouping/clouds for each model)
  stat_ellipse(data = df_raw, aes(x = recall, y = precision, fill = method, color = method),
               geom = "polygon", alpha = 0.08, linewidth = 0.3, level = 0.80) +
  
  # D. Mean Points (White outline layer to pop)
  geom_point(data = df_mean, aes(x = recall, y = precision, shape = method), 
             color = "white", size = 6, stroke = 2) +
  # Mean Points (Actual color)
  geom_point(data = df_mean, aes(x = recall, y = precision, color = method, shape = method), 
             size = 4, stroke = 1.2) +
  
  # E. NEW: Direct Labeling (Method Name + F1 Score)
  geom_label_repel(data = df_mean, 
                   aes(x = recall, y = precision, 
                       label = paste0(method, "\n(F1: ", sprintf("%.2f", f1), ")"), 
                       color = method), 
                   size = 2.5, family = base_font, fontface = "bold", 
                   fill = alpha("white", 0.90), label.size = NA, 
                   box.padding = 0.6, point.padding = 0.5, 
                   segment.color = "#AAAAAA", segment.size = 0.5,
                   min.segment.length = 0, # Forces leader lines to always draw
                   show.legend = FALSE) +
  
  # F. NEW: "Ideal Target" Annotation at (1,1)
  annotate("point", x = 1, y = 1, shape = 13, size = 6, color = "#222222", stroke = 1) +
  annotate("text", x = 0.96, y = 1.03, label = "Ideal", size = 3, family = base_font, fontface = "italic", color = "#222222") +
  
  # G. Scales & Square Aspect Ratio
  scale_color_manual(values = hex_colors) +
  scale_fill_manual(values = hex_colors) +
  scale_shape_manual(values = shapes_map) +
  coord_fixed(ratio = 1, xlim = c(0, 1.08), ylim = c(0, 1.08), expand = FALSE, clip = "off") +
  
  # H. Faceting
  facet_wrap(~Domain) +
  
  # I. Theming
  labs(
    title = "Precision-Recall Analysis: Drug vs Gene Synonym Retrieval",
    subtitle = "Shaded regions represent 80% confidence ellipses • Dashed curves = F1 iso-lines",
    x = "Recall",
    y = "Precision"
  ) +
  theme_minimal(base_family = base_font) +
  theme(
    plot.title = element_text(face = "bold", size = 15, hjust = 0.5, margin = margin(b = 5)),
    plot.subtitle = element_text(color = "#666666", size = 9, hjust = 0.5, margin = margin(b = 20)),
    plot.background = element_rect(fill = "white", color = NA),
    panel.background = element_rect(fill = "#F9FAFB", color = NA),
    panel.grid.major = element_line(color = "white", linewidth = 1.2),
    panel.grid.minor = element_blank(),
    panel.border = element_rect(color = "#DDDDDD", fill = NA, linewidth = 1), # Sharp border around plots
    panel.spacing = unit(2, "lines"),
    strip.text = element_text(face = "bold", size = 12, margin = margin(b = 10, t = 10)),
    strip.background = element_rect(fill = "#EFEFEF", color = NA), # Beautiful grey header for facets
    axis.title = element_text(face = "bold", size = 10),
    axis.text = element_text(color = "#444444", size = 8),
    legend.position = "none" # Removed legend entirely since we use direct labeling!
  )

# 6. Save Plot
ggsave("pr_plot_premium.pdf", plot = p, width = 240, height = 130, units = "mm", device = cairo_pdf)

print("Export complete: Generated 'pr_plot_premium.pdf'")