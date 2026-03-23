library(ggplot2)
library(ggridges)
library(dplyr)
library(readr)
library(extrafont) # For Arial

# Load Data
df <- read_excel("~/abhi/biochirp/evaluation/Agentic_SQL/nl2SQL_result.xlsx")

# Calculate BioChirp Median
biochirp_median <- df %>%
  filter(grepl("BioChirp", Framework, ignore.case = TRUE)) %>%
  summarise(med = median(latency)) %>%
  pull(med)

# Define Matte Colors
matte_colors <- c("#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7")

# Create Plot
p <- ggplot(df, aes(x = latency, y = Framework, fill = Framework, color = Framework)) +
  
  # 1. Density Plot (Top Half)
  # scale < 1 ensures NO OVERLAP between rows
  geom_density_ridges(
    scale = 0.9,             # < 1 means no overlap
    rel_min_height = 0.01,
    size = 0.2,
    alpha = 0.7              # Matte transparency
  ) +
  
  # 2. Points Below Distribution (Raincloud Style)
  # jittered_points = TRUE puts points at the bottom
  # position = position_points_jitter(...) controls the scatter
  geom_density_ridges(
    scale = 0.9,
    jittered_points = TRUE,
    position = position_points_jitter(width = 0, height = 0), # Jitter in y-direction is handled by side
    point_shape = '|',       # Tick marks or small dots
    point_size = 0.5,
    point_alpha = 0.6,
    alpha = 0                # Invisible density, only points
  ) +
  
  # Reference Line
  geom_vline(xintercept = biochirp_median, linetype = "dashed", color = "#2ecc71", size = 0.3) +
  
  # Colors
  scale_fill_manual(values = matte_colors) +
  scale_color_manual(values = matte_colors) +
  
  # Theme
  theme_minimal(base_size = 7, base_family = "Arial") +
  theme(
    # REMOVE ALL GRID LINES
    panel.grid.major = element_blank(),
    panel.grid.minor = element_blank(),
    
    # Axis Lines (The solid lines on Left and Bottom)
    axis.line.x = element_line(color = "black", size = 0.2),
    axis.line.y = element_line(color = "black", size = 0.2),
    
    # Text
    axis.text = element_text(color = "black", size = 7),
    axis.title = element_text(color = "black", size = 7, face = "bold"),
    
    # Remove Legend and Y-axis title
    legend.position = "none",
    axis.title.y = element_blank(),
    
    # Margins for 50x50mm
    plot.margin = margin(2, 2, 2, 2, "mm")
  ) +
  
  labs(x = "Latency (s)")

# Save as Vector PDF (50x50 mm)
ggsave("~/abhi/biochirp/evaluation/Agentic_SQL/latency_raincloud_50x50.pdf", plot = p, width = 50, height = 50, units = "mm", device = cairo_pdf)