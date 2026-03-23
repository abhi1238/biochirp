# ------------------------------------------------------------------------------
# R CODE: CORRECTED BIOCHIRP COLOR (Biochirp = #5BBCBF)
# ------------------------------------------------------------------------------

if (!require("pacman")) install.packages("pacman")
pacman::p_load(ggplot2, dplyr, readxl, stringr, cowplot, scales, grid, extrafont)

# 1. DATA PREP
df <- read_excel("~/abhi/biochirp/evaluation/MCP/result_mcp.xlsx")

df_clean <- df %>%
  mutate(
    Count = as.numeric(as.character(Count)),
    Latency = as.numeric(as.character(Latency)),
    Error = tidyr::replace_na(Error, "Pass"),
    Type_Clean = tolower(str_trim(Type))
  ) %>%
  filter(
    tolower(Error) == "pass",
    !is.na(Count) & Count > 0,
    !is.na(Latency) & Latency > 0
  ) %>%
  mutate(
    Log_Latency = log10(Latency),
    Log_Count = log10(Count),
    Style_Group = case_when(
      grepl("agentic", Type_Clean) ~ "Agentic",
      grepl("chat", Type_Clean) | grepl("claude", Type_Clean) ~ "Chat Endpoint",
      grepl("biochirp", Type_Clean) ~ "BioChirp",
      TRUE ~ "Other"
    )
  )

# Calculate Centroids
df_means <- df_clean %>%
  group_by(Model, Style_Group) %>%
  summarise(
    Mean_Log_Latency = mean(Log_Latency, na.rm = TRUE),
    Mean_Log_Count = mean(Log_Count, na.rm = TRUE),
    .groups = 'drop'
  )

# 2. LIMITS
x_limits <- range(df_clean$Log_Latency)
y_limits <- range(df_clean$Log_Count)
x_padding <- diff(x_limits) * 0.05
y_padding <- diff(y_limits) * 0.05
x_limits <- c(x_limits[1] - x_padding, x_limits[2] + x_padding)
y_limits <- c(y_limits[1] - y_padding, y_limits[2] + y_padding)

# 3. STYLING & COLORS
# FIXED: "Biochirp" (lowercase c) to match data
custom_colors <- c(
  "Biochirp"     = "#5BBCBF", # CORRECTED KEY
  "gpt-5-mini"   = "#E76F51", 
  "gpt-4o-mini"  = "#F4A261", 
  "gpt-4.1-mini" = "#E9C46A", 
  "gpt-5-nano"   = "#264653", 
  "gpt-4.1-nano" = "#2A9D8F", 
  "Haiku 3.5"    = "#457B9D", 
  "Sonnet 4.5"   = "#8E44AD"
)

custom_shapes <- c("Agentic" = 8, "Chat Endpoint" = 16, "BioChirp" = 3, "Other" = 16)
custom_linetypes <- c("Agentic" = "dashed", "Chat Endpoint" = "solid", "BioChirp" = "dotted", "Other" = "solid")

common_theme <- theme_minimal(base_family = "Arial", base_size = 7) +
  theme(
    panel.grid = element_blank(),
    axis.line.x.bottom = element_line(color = "black", size = 0.3),
    axis.line.y.left   = element_line(color = "black", size = 0.3),
    panel.border = element_blank(),
    axis.ticks = element_line(color = "black", size = 0.3),
    plot.margin = margin(0, 0, 0, 0)
  )

# 4. PLOT COMPONENTS

# A. Main Plot
p_main <- ggplot(df_clean, aes(x = Log_Latency, y = Log_Count, color = Model)) +
  geom_smooth(aes(linetype = Style_Group), method = "lm", se = FALSE, size = 0.4, alpha = 0.5) +
  geom_point(aes(shape = Style_Group), size = 0.8, stroke = 0.4, alpha = 0.6) +
  geom_point(data = df_means, aes(x = Mean_Log_Latency, y = Mean_Log_Count, shape = Style_Group), 
             size = 2.0, stroke = 0.6, alpha = 1.0) +
  annotate("text", x = min(df_clean$Log_Latency), y = max(df_clean$Log_Count), 
           label = "High Yield\nFast Retrieval", hjust = 0, vjust = 1, 
           size = 2.2, color = "grey40", fontface = "italic", family = "Arial") +
  scale_shape_manual(name = "Architecture", values = custom_shapes) +
  scale_linetype_manual(name = "Architecture", values = custom_linetypes) +
  scale_color_manual(name = "Model", values = custom_colors) + 
  scale_x_continuous(limits = x_limits, expand = c(0, 0)) +
  scale_y_continuous(limits = y_limits, expand = c(0, 0)) +
  labs(x = "Log10(Latency) [s]", y = "Log10(Count)") +
  common_theme +
  theme(legend.position = "none")

# B. Top Density
p_top <- ggplot(df_clean, aes(x = Log_Latency, fill = Model, color = Model)) +
  geom_density(alpha = 0.4, size = 0.2) +
  scale_fill_manual(values = custom_colors) +
  scale_color_manual(values = custom_colors) +
  scale_x_continuous(limits = x_limits, expand = c(0, 0)) +
  theme_void() +
  theme(legend.position = "none", plot.margin = margin(0,0,0,0))

# C. Right Density
p_right <- ggplot(df_clean, aes(x = Log_Count, fill = Model, color = Model)) +
  geom_density(alpha = 0.4, size = 0.2) +
  coord_flip() + 
  scale_fill_manual(values = custom_colors) +
  scale_color_manual(values = custom_colors) +
  scale_x_continuous(limits = y_limits, expand = c(0, 0)) +
  theme_void() +
  theme(legend.position = "none", plot.margin = margin(0,0,0,0))

# D. Legend
p_legend_source <- ggplot(df_clean, aes(x = Log_Latency, y = Log_Count, color = Model)) +
  geom_point(aes(shape = Style_Group), size = 2) +
  geom_line(aes(linetype = Style_Group), size = 1) +
  scale_shape_manual(name = "Architecture", values = custom_shapes) +
  scale_linetype_manual(name = "Architecture", values = custom_linetypes) +
  scale_color_manual(name = "Model", values = custom_colors) +
  theme_minimal(base_family = "Arial", base_size = 7) +
  theme(
    legend.box = "vertical",
    legend.title = element_text(face = "bold", size = 6),
    legend.text = element_text(size = 5.5),
    legend.key.size = unit(3, "mm"),
    legend.spacing = unit(1, "mm")
  )
legend <- cowplot::get_legend(p_legend_source)

# 5. LAYOUT & EXPORT
plot_grid_aligned <- cowplot::plot_grid(
  p_top, NULL, 
  p_main, p_right, 
  align = "hv", axis = "tblr", 
  rel_widths = c(4, 1), 
  rel_heights = c(1, 4),
  ncol = 2
)

final_plot <- cowplot::plot_grid(
  plot_grid_aligned, legend,
  rel_widths = c(5, 1),
  nrow = 1
)

title <- ggdraw() + 
  draw_label("Efficiency Analysis", fontfamily = "Arial", fontface = "bold", size = 8, x = 0.05, hjust = 0)

final_with_title <- plot_grid(title, final_plot, ncol = 1, rel_heights = c(0.1, 1))

ggsave("~/abhi/biochirp/evaluation/MCP/figure/B/retrival_plot.pdf", plot = final_with_title, width = 100, height = 75, units = "mm", device = cairo_pdf)