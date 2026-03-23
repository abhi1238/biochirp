library(ggplot2)
library(ggridges)
library(dplyr)
library(tidyr)
library(readxl) 
library(extrafont)

# 1. Load Data
df <- read_excel("~/abhi/biochirp/evaluation/MCQ/Total_result_mcq.xlsx")

# 2. Reshape & Rename Models
df_clean <- df %>%
  pivot_longer(
    cols = c(time_taken_run_1, time_taken_run_2, time_taken_run_3),
    names_to = "run",
    values_to = "latency"
  ) %>%
  filter(!is.na(latency)) %>% 
  mutate(model = case_when(
    model == "biochirp" ~ "biochirp",
    model == "gpt-5-mini" ~ "gpt-5-mini",
    model == "gpt-4.1-nano" ~ "gpt-4.1",
    model == "gpt-5-nano" ~ "gpt-5-nano",
    model == "llama-3.3-70b-versatile" ~ "llama-3.3",
    model == "gemini-2.5-flash-lite" ~ "gemini-2.5",
    model == "biochatter(gpt-4.1-nano)" ~ "biochatter(gpt-4)",
    model == "biochatter(gpt-5-nano)" ~ "biochatter(gpt-5)",
    model == "grok-4-1-fast-non-reasoning-latest" ~ "grok-4-1",
    TRUE ~ model 
  ))

# 3. Calculate Stats for BioChirp (Reference Line)
bio_stats <- df_clean %>%
  filter(model == "biochirp") %>%
  summarise(median = median(latency))

# 4. Sort Models (Fastest on Top)
df_sorted <- df_clean %>%
  mutate(model = reorder(model, latency, FUN = median)) %>%
  mutate(model = factor(model, levels = rev(levels(model))))

# 5. Define Colors
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

# 6. Create Plot
p <- ggplot(df_sorted, aes(x = latency, y = model)) +
  
  # --- Reference Line ---
  geom_vline(xintercept = bio_stats$median, linetype = "dashed", 
             color = "#5BBCBF", linewidth = 0.25) +
  
  # --- Clean Distribution Ridges ---
  geom_density_ridges(
    aes(fill = model, color = model),
    scale = 0.9,               
    rel_min_height = 0,        
    trim = FALSE,              
    quantile_lines = TRUE,
    quantiles = 2,             
    jittered_points = FALSE,   
    linewidth = 0.25,              
    alpha = 0.7
  ) +
  
  # --- Logarithmic X-Axis ---
  # Using log10 scale to better visualize wide variations in latency
  scale_x_log10(
    expand = expansion(mult = c(0.01, 0.05)),
    breaks = scales::trans_breaks("log10", function(x) 10^x),
    labels = scales::trans_format("log10", scales::math_format(10^.x))
  ) + 
  
  # --- Colors ---
  scale_fill_manual(values = custom_colors) +
  scale_color_manual(values = custom_colors) +
  
  # --- Theme (Arial Size 7, No Grids, No Y-Labels) ---
  theme_minimal(base_size = 7, base_family = "Arial") +
  theme(
    panel.grid = element_blank(),
    
    axis.title.y = element_blank(),
    axis.text.y = element_blank(),     
    axis.ticks.y = element_blank(),    
    
    axis.title.x = element_text(face = "bold", margin = margin(t = 4)),
    axis.text.x = element_text(color = "#2c3e50", size = 7), 
    axis.line.x = element_line(color = "#2c3e50", linewidth = 0.25),
    axis.line.y = element_line(color = "#2c3e50", linewidth = 0.25),
    axis.ticks.x = element_line(color = "#2c3e50", linewidth = 0.25),
    
    legend.position = "none",
    legend.title = element_blank(),
    legend.text = element_text(size = 7),
    legend.key.size = unit(3, "mm"),
    
    plot.margin = margin(2, 2, 2, 2, "mm")
  ) +
  labs(x = "Latency (s, log scale)")

# 7. Save as Vector PDF
ggsave("~/abhi/biochirp/evaluation/MCQ/mcq_latency_log_final.pdf", 
       plot = p, width = 60, height = 60, units = "mm", device = cairo_pdf)