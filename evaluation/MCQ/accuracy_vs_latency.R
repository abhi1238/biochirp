library(ggplot2)
library(dplyr)
library(tidyr)
library(ggrepel)
library(extrafont)
library(readxl)

# 1. Load and Process Data (Keeping your specific path)
raw_df <- read_excel("~/abhi/biochirp/evaluation/MCQ/Total_result_mcq.xlsx")

df_stats <- raw_df %>%
  group_by(model) %>%
  summarise(
    Latency = median(c(time_taken_run_1, time_taken_run_2, time_taken_run_3), na.rm = TRUE),
    Accuracy = mean(c(is_correct_run_1, is_correct_run_2, is_correct_run_3), na.rm = TRUE)
  ) %>%
  mutate(model_clean = case_when(
    model == "biochatter(gpt-4.1-nano)" ~ "biochatter(gpt-4)",
    model == "biochatter(gpt-5-nano)" ~ "biochatter(gpt-5)",
    model == "grok-4-1-fast-non-reasoning-latest" ~ "grok-4-1",
    model == "llama-3.3-70b-versatile" ~ "llama-3.3",
    model == "gemini-2.5-flash-lite" ~ "gemini-2.5",
    model == "gpt-4.1-nano" ~ "gpt-4.1",
    TRUE ~ model
  ))

# 2. Fit Regression and Extract m (Slope) and c (Intercept)
fit <- lm(Accuracy ~ log10(Latency), data = df_stats)
intercept_val <- coef(fit)[1]
slope_val     <- coef(fit)[2]

# Format Equation and Slope text
eq_text <- paste0("y = ", round(slope_val, 3), " * log10(x) + ", round(intercept_val, 3))
slope_per_decade <- paste0("Slope: ", round(slope_val * 100, 1), "% accuracy gain per 10x latency")

# 3. Define Custom Colors
custom_colors <- c(
  "biochirp"          = "#5BBCBF",
  "gpt-5-mini"        = "#D55E00",
  "biochatter(gpt-4)" = "#E69F00",
  "biochatter(gpt-5)" = "#0072B2",
  "llama-3.3"         = "#56B4E9",
  "gpt-4.1"           = "#CC79A7",
  "gpt-5-nano"        = "#009E73",
  "grok-4-1"          = "#F0E442",
  "gemini-2.5"        = "#999999"
)

# 4. Create the Plot
p <- ggplot(df_stats, aes(x = Latency, y = Accuracy)) +
  
  # A. The Least Square Regression Line (Dot-Dash)
  geom_smooth(method = "lm", formula = y ~ log10(x), 
              color = "grey50", fill = "grey90", 
              linetype = "dotdash", # Dotted-dashed line style
              linewidth = 0.6, alpha = 0.1) +
  
  # B. Median Points
  geom_point(aes(color = model_clean), size = 3, alpha = 0.9) +
  
  # C. Labels
  geom_text_repel(aes(label = model_clean, color = model_clean),
                  size = 2.2, fontface = "bold", box.padding = 0.4, show.legend = FALSE) +
  
  # D. Display Slope and Intercept (c)
  annotate("text", x = 0.15, y = 1.02, label = eq_text, 
           hjust = 0, size = 2.8, fontface = "bold", color = "black", family = "Arial") +
  annotate("text", x = 0.15, y = 0.99, label = slope_per_decade, 
           hjust = 0, size = 2.4, color = "grey40", family = "Arial") +
  
  # E. Scales
  scale_color_manual(values = custom_colors, name = "Models") +
  scale_x_log10(breaks = c(0.1, 0.5, 1, 5, 10, 20),
                labels = c("0.1s", "0.5s", "1s", "5s", "10s", "20s"),
                expand = expansion(mult = c(0.1, 0.1))) +
  scale_y_continuous(limits = c(0.60, 1.05), breaks = seq(0.60, 1.00, by = 0.05),
                     labels = scales::percent_format(accuracy = 1)) +
  
  # F. Theme and Styling (REMOVING ALL GRID LINES)
  theme_minimal(base_size = 7, base_family = "Arial") +
  theme(
    legend.position = "none",
    # Turn off all axis grids
    panel.grid.major = element_blank(), 
    panel.grid.minor = element_blank(),
    # Keep axis lines and ticks for clarity
    axis.line = element_line(color = "black", linewidth = 0.25),
    axis.ticks = element_line(color = "black", linewidth = 0.25),
    axis.title = element_text(face = "bold"),
    plot.title = element_text(face = "bold", size = 9, hjust = 0.5)
  ) +
  labs(title = "Trade-off Analysis: Accuracy vs. Latency",
       x = "Median Latency (Seconds, Log Scale)",
       y = "Overall Accuracy (%)")

# 5. Save (Keeping your specific output path)
ggsave("~/abhi/biochirp/evaluation/MCQ/accuracy_latency.pdf", 
       plot = p, width = 60, height = 60, units = "mm", device = cairo_pdf)