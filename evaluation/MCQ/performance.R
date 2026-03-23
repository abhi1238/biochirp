library(ggplot2)
library(dplyr)
library(tidyr)
library(ggrepel)
library(extrafont)

# 1. Data Setup
df <- tibble(
  model = c("biochirp", "gpt-5-mini", "biochatter(gpt-5)", "gpt-5-nano",
            "grok-4-1", "llama-3.3", "biochatter(gpt-4)", "gpt-4.1", "gemini-2.5"),
  run1 = c(0.95, 0.92, 0.92, 0.92, 0.74, 0.73, 0.70, 0.65, 0.65),
  run2 = c(0.95, 0.92, 0.94, 0.91, 0.74, 0.73, 0.70, 0.69, 0.68),
  run3 = c(0.94, 0.92, 0.90, 0.90, 0.74, 0.73, 0.68, 0.69, 0.68)
)

# 2. Reshape & Label Strategy
df_long <- df %>%
  pivot_longer(cols = starts_with("run"), names_to = "Run", values_to = "Accuracy") %>%
  mutate(
    Run = factor(Run, levels = c("run1", "run2", "run3"), labels = c("Run 1", "Run 2", "Run 3")),
    is_target = ifelse(model == "biochirp", "Target", "Competitor"),
    lty = ifelse(model == "biochirp", "solid", "22")
  )

# Labels anchored at Run 2 for staggered top/bottom effect
df_labels <- df_long %>% 
  filter(Run == "Run 2") %>%
  mutate(
    v_nudge = ifelse(Accuracy > 0.85, 0.05, -0.05)
  )

# 3. Create the Plot
p <- ggplot(df_long, aes(x = Run, y = Accuracy, group = model, color = model)) +
  
  # A. Subtle Background "Glow" for BioChirp
  geom_line(data = filter(df_long, is_target == "Target"), 
            linewidth = 2, color = "#5BBCBF", alpha = 0.15) +
  
  # B. Main Lines
  geom_line(aes(linewidth = is_target, linetype = lty), alpha = 0.8) +
  
  # C. Staggered Labels (Top and Below)
  geom_text_repel(
    data = df_labels,
    aes(label = model, y = Accuracy),
    nudge_y = df_labels$v_nudge,
    direction = "y",
    hjust = 0.5,
    size = 2.2,
    family = "Arial",
    fontface = "bold",
    segment.size = 0.2,
    segment.color = "grey70",
    segment.alpha = 0.5,
    box.padding = 0.15
  ) +
  
  # D. Points
  geom_point(aes(size = is_target), shape = 19) + 
  
  # E. Color Palette
  scale_color_manual(values = c(
    "biochirp"          = "#5BBCBF",
    "gpt-5-mini"        = "#D55E00",
    "biochatter(gpt-4)" = "#E69F00",
    "biochatter(gpt-5)" = "#0072B2",
    "llama-3.3"         = "#56B4E9",
    "gpt-4.1"           = "#CC79A7",
    "gpt-5-nano"        = "#009E73",
    "grok-4-1"          = "#F0E442",
    "gemini-2.5"        = "#999999"
  )) +
  
  # F. Scales & Axis Formatting (5% Step)
  scale_linewidth_manual(values = c("Target" = 1.0, "Competitor" = 0.3), guide = "none") + 
  scale_size_manual(values = c("Target" = 1.6, "Competitor" = 0.6), guide = "none") + 
  scale_linetype_identity(guide = "none") +
  
  scale_y_continuous(
    limits = c(0.55, 1.05), 
    breaks = seq(0.60, 1.00, by = 0.05), # <--- 5% Steps
    labels = scales::percent_format(accuracy = 1),
    expand = c(0, 0)
  ) +
  
  # G. Theme with X-axis Grid
  theme_minimal(base_size = 7, base_family = "Arial") +
  theme(
    legend.position = "none",
    
    # Grid lines - Restored X-axis Grid
    panel.grid.major.x = element_line(color = "grey94", linewidth = 0.2), # <--- X-grid added
    panel.grid.major.y = element_line(color = "grey94", linewidth = 0.2),
    panel.grid.minor = element_blank(),
    
    # Visible Left Y-axis and X-ticks
    axis.line.y = element_line(color = "black", linewidth = 0.25),
    axis.line.x = element_line(color = "black", linewidth = 0.25), 
    axis.ticks = element_line(color = "black", linewidth = 0.25),
    axis.ticks.length = unit(0.8, "mm"),
    
    axis.title = element_text(face = "bold", size = 7),
    axis.text = element_text(color = "black", size = 7),
    plot.title = element_text(face = "bold", size = 9, hjust = 0.5),
    plot.margin = margin(5, 5, 5, 5, "mm")
  ) +
  labs(
    title = "Accuracy Stability Benchmark",
    y = "Performance Accuracy",
    x = NULL
  )

# 4. Save
ggsave("~/abhi/biochirp/evaluation/MCQ/accuracy_stability.pdf", 
       plot = p, width = 60, height = 60, units = "mm", device = cairo_pdf)