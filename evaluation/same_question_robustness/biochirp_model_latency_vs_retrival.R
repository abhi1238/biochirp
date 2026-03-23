# =============================================================================
# Premium Regression Plot | 120 x 80 mm 
# x = Latency, y = Rows Retrieved | Least Squares (lm)
# Models: ttd, ctd, hcdt, OpenTargets
# Style: Pure Black Axes, Arial 6pt, Bottom Legend. Editable Vectors (PDF/SVG)
# =============================================================================

library(ggplot2)
library(dplyr)
library(scales)
library(svglite)

# ── 1. Load Data ─────────────────────────────────────────────────────────────
df <- read.csv("~/abhi/biochirp/evaluation/same_question_robustness/ridgeline_data.csv")

# ── 2. Filter for Specific Models and Clean Data ─────────────────────────────
# Note: Using "OpenTargets" to match the previous dictionary keys you provided
target_models <- c("ttd", "ctd", "hcdt", "OpenTargets", "OpenTarget") 

df_filtered <- df %>%
  filter(!is.na(rows), !is.na(latency),
         !is.infinite(rows), !is.infinite(latency),
         rows > 0, latency > 0,
         model %in% target_models) %>%
  # Consolidate naming just in case "OpenTarget" vs "OpenTargets" appears
  mutate(model = ifelse(model == "OpenTarget", "OpenTargets", as.character(model))) %>%
  mutate(model = factor(model, levels = c("ttd", "ctd", "hcdt", "OpenTargets")))

# ── 3. Color Palette ─────────────────────────────────────────────────────────
# Assigning 4 distinct, high-contrast colors for the target models
model_colors <- c(
  "ttd"         = "#1f77b4", # Blue
  "ctd"         = "#ff7f0e", # Orange
  "hcdt"        = "#2ca02c", # Green
  "OpenTargets" = "#d62728"  # Red
)

# ── 4. Create Plot ───────────────────────────────────────────────────────────
p <- ggplot(df_filtered, aes(x = latency, y = rows, color = model, fill = model)) +
  
  # Scatter plot points (semi-transparent so dense clusters are visible)
  geom_point(alpha = 0.4, size = 0.6, stroke = 0) +
  
  # Least Squares Regression Line (method = "lm") with 95% Confidence Interval ribbon
  geom_smooth(method = "lm", formula = y ~ x, se = TRUE, alpha = 0.2, linewidth = 0.6) +
  
  # Log scales for both axes using scientific math formatting
  scale_x_log10(
    breaks = trans_breaks("log10", function(x) 10^x),
    labels = trans_format("log10", math_format(10^.x))
  ) +
  scale_y_log10(
    breaks = trans_breaks("log10", function(x) 10^x),
    labels = trans_format("log10", math_format(10^.x))
  ) +
  
  scale_color_manual(values = model_colors) +
  scale_fill_manual(values = model_colors) +
  
  labs(
    x = "Latency (seconds, log scale)", 
    y = "Entries Retrieved (log scale)"
  ) +
  
  # ── 5. Strict Arial 6pt & Pure Black Axes Theme ────────────────────────────
  theme_minimal(base_size = 6, base_family = "Arial") +
  theme(
    # Background Grid
    panel.grid.major = element_line(color = "grey90", linewidth = 0.25),
    panel.grid.minor = element_blank(),
    
    # Strict Pure Black Axes & Text
    axis.title = element_text(color = "#000000", size = 6, face = "bold"),
    axis.text  = element_text(color = "#000000", size = 6),
    
    # Explicit Black Lines for X and Y axes
    axis.line.x.bottom = element_line(color = "#000000", linewidth = 0.5),
    axis.line.y.left   = element_line(color = "#000000", linewidth = 0.5),
    axis.ticks         = element_line(color = "#000000", linewidth = 0.5),
    axis.ticks.length  = unit(2, "pt"),
    
    plot.margin = margin(4, 4, 4, 4, "pt"),
    
    # Bottom Legend Formatting
    legend.position = "bottom",
    legend.key.size = unit(3, "mm"),
    legend.text     = element_text(size = 6, color = "#000000"),
    legend.title    = element_blank(),
    legend.margin   = margin(t = 0, b = 0)
  )

# ── 6. Export ────────────────────────────────────────────────────────────────
ggsave("~/abhi/biochirp/evaluation/same_question_robustness/regression_latency_vs_rows_120x80.pdf", plot = p, width = 120, height = 80, units = "mm", device = cairo_pdf)
ggsave("~/abhi/biochirp/evaluation/same_question_robustness/regression_latency_vs_rows_120x80.svg", plot = p, width = 120, height = 80, units = "mm", device = svglite)

message("Saved linear regression plot (Latency vs. Rows) for target models to PDF and SVG.")