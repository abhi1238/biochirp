# =============================================================================
# Premium Ridgeline Plot | 120 x 80 mm | Sorted Lowest Median at TOP
# Style: Black Individual Baselines, No Y-Labels, Bottom Legend
# X-Axis: Pure Black, Arial 6pt. Editable Vectors (PDF/SVG)
# =============================================================================

library(ggplot2)
library(dplyr)
library(patchwork)
library(scales)
library(svglite)

# ── 1. Load Data ─────────────────────────────────────────────────────────────
df <- read.csv("~/abhi/biochirp/evaluation/same_question_robustness/ridgeline_data.csv")

df_clean <- df %>%
  filter(!is.na(rows), !is.na(latency),
         !is.infinite(rows), !is.infinite(latency),
         rows > 0, latency > 0) %>%
  mutate(model = as.character(model), run = as.character(run))

# ── 2. Sort Models by Median Retrieval Count (Lowest at TOP) ─────────────────
model_medians <- df_clean %>%
  group_by(model) %>%
  summarize(med_rows = median(rows, na.rm = TRUE)) %>%
  arrange(desc(med_rows)) # Highest median first (bottom), lowest last (top)

# The first level (highest median) gets plotted at y_base = 0 (the bottom)
model_order <- model_medians$model 
df_clean$model <- factor(df_clean$model, levels = model_order)

# ── 3. Density Engine: Individual Runs + Median (No Ribbons) ─────────────────
compute_spaghetti_densities <- function(data, value_col, scale_height = 0.95) {
  
  vals <- data[[value_col]]
  log_min <- log10(min(vals))
  log_max <- log10(max(vals))
  x_grid <- seq(log_min - 0.1, log_max + 0.1, length.out = 300)
  
  run_list <- list()
  
  for (m in unique(data$model)) {
    for (r in unique(data$run)) {
      sub_df <- data %>% filter(model == m, run == r)
      v <- sub_df[[value_col]]
      if (length(v) > 1) {
        d <- density(log10(v), from = min(x_grid), to = max(x_grid), n = 300)
        run_list[[length(run_list) + 1]] <- data.frame(
          model = m, run = r, x_real = 10^x_grid, den = d$y
        )
      }
    }
  }
  
  df_runs <- bind_rows(run_list)
  
  df_meds <- df_runs %>%
    group_by(model, x_real) %>%
    summarize(med_den = median(den, na.rm = TRUE), .groups = "drop")
  
  max_peak <- max(df_runs$den, na.rm = TRUE)
  
  df_runs <- df_runs %>%
    mutate(
      scaled_den = (den / max_peak) * scale_height,
      # Base aligns directly with the factor levels (level 1 = y_base 0)
      y_base = as.numeric(factor(model, levels = model_order)) - 1
    )
  
  df_meds <- df_meds %>%
    mutate(
      scaled_med = (med_den / max_peak) * scale_height,
      y_base = as.numeric(factor(model, levels = model_order)) - 1
    )
  
  return(list(runs = df_runs, meds = df_meds))
}

dens_rows <- compute_spaghetti_densities(df_clean, "rows")
dens_lat  <- compute_spaghetti_densities(df_clean, "latency")

# ── 4. Color Palette & Strict 6pt Theme ──────────────────────────────────────
model_colors <- c(
  "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", 
  "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"
)
names(model_colors) <- levels(df_clean$model)

y_breaks <- 0:(length(model_order) - 1)
y_max_limit <- max(y_breaks) + 1.2 

theme_premium_ridge <- function() {
  theme_minimal(base_size = 6, base_family = "Arial") +
    theme(
      panel.grid.major.x = element_blank(),
      panel.grid.minor.x = element_blank(),
      
      # Pure Black Baselines for each individual model
      panel.grid.major.y = element_line(color = "#000000", linewidth = 0.3), 
      panel.grid.minor.y = element_blank(),
      
      # Completely remove Y-Axis Labels & Ticks
      axis.title.y = element_blank(),
      axis.text.y  = element_blank(),
      axis.ticks.y = element_blank(),
      
      # Strict Pure Black, Arial 6pt Bottom X-Axis
      axis.title.x = element_text(color = "#000000", size = 6, margin = margin(t = 4)),
      axis.text.x  = element_text(color = "#000000", size = 6),
      axis.line.x.bottom = element_line(color = "#000000", linewidth = 0.5),
      axis.ticks.x       = element_line(color = "#000000", linewidth = 0.5),
      axis.ticks.length.x= unit(2, "pt"),
      
      plot.title  = element_text(size = 6, face = "bold", margin = margin(b = 4)),
      plot.margin = margin(2, 4, 2, 2, "pt"),
      
      # Bottom Legend Formatting
      legend.position = "bottom",
      legend.key.size = unit(3, "mm"),
      legend.text     = element_text(size = 6, color = "#000000"),
      legend.title    = element_blank(),
      legend.margin   = margin(t = 0, b = 0)
    )
}

# ── 5. Create Panel A: Rows Retrieved ────────────────────────────────────────
pA <- ggplot() +
  # Thinner run lines (0.15)
  geom_line(data = dens_rows$runs, aes(x = x_real, y = y_base + scaled_den, group = interaction(model, run), color = model), alpha = 0.3, linewidth = 0.15) +
  # Thinner median line (0.35)
  geom_line(data = dens_rows$meds, aes(x = x_real, y = y_base + scaled_med, color = model), linewidth = 0.35) +
  
  scale_x_log10(
    breaks = trans_breaks("log10", function(x) 10^x),
    labels = trans_format("log10", math_format(10^.x)),
    expand = expansion(mult = c(0.01, 0.05))
  ) +
  scale_y_continuous(breaks = y_breaks, limits = c(0, y_max_limit), expand = c(0,0)) +
  scale_color_manual(values = model_colors) +
  labs(title = "A. Entries Retrieved", x = "Rows retrieved (log scale)") +
  theme_premium_ridge()

# ── 6. Create Panel B: Latency ───────────────────────────────────────────────
pB <- ggplot() +
  geom_line(data = dens_lat$runs, aes(x = x_real, y = y_base + scaled_den, group = interaction(model, run), color = model), alpha = 0.3, linewidth = 0.15) +
  geom_line(data = dens_lat$meds, aes(x = x_real, y = y_base + scaled_med, color = model), linewidth = 0.35) +
  
  scale_x_log10(
    breaks = trans_breaks("log10", function(x) 10^x),
    labels = trans_format("log10", math_format(10^.x)),
    expand = expansion(mult = c(0.01, 0.05))
  ) +
  scale_y_continuous(breaks = y_breaks, limits = c(0, y_max_limit), expand = c(0,0)) +
  scale_color_manual(values = model_colors) +
  labs(title = "B. Latency", x = "Latency (seconds, log scale)") +
  theme_premium_ridge()

# ── 7. Combine and Export ────────────────────────────────────────────────────
# Use patchwork to bind them and share the exact same legend at the bottom
final_plot <- pA + pB + plot_layout(guides = "collect") & theme(legend.position = "bottom")

ggsave("ridgeline_lowest_top_120x80.pdf", plot = final_plot, width = 120, height = 80, units = "mm", device = cairo_pdf)
ggsave("ridgeline_lowest_top_120x80.svg", plot = final_plot, width = 120, height = 80, units = "mm", device = svglite)

message("Saved purely aligned ridgeline with the lowest median at the TOP to PDF and SVG.")