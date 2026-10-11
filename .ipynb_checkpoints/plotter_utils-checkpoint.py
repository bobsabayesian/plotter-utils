import vpype
import subprocess
from IPython.display import SVG, display

def preview_svg(filename):
    """
    Renders an SVG file directly inside the Jupyter Notebook.
    """
    display(SVG(filename))

# ==============================================================================
# FUNCTION: generate_registration_ticks
# ==============================================================================
# Generates an SVG with diagonal registration marks pointing inward from the 
# 4 corners of the specified page size.
#
# HOW TO SPECIFY SIZES:
# 1. Standard Sizes: Pass the standard name (case-insensitive).
#    Examples: "a1", "a3", "letter", "legal"
#
# 2. Custom Sizes: Pass a string formatted as "WIDTHxHEIGHT[unit]". 
#    Do NOT use spaces. Supported units: in, cm, mm, px, pc, pt.
#    Examples: 
#      - "24x36in"  (24 inches wide, 36 inches tall)
#      - "50x50cm"  (50 centimeters square)
#      - "400x600mm"(400 mm wide, 600 mm tall)
#
# tick_length: The distance the line travels inward on BOTH the X and Y axes.
#              Defaults to "2mm", but accepts any standard unit (e.g., "0.25in").
# ==============================================================================

def generate_registration_ticks(paper_size="a1", tick_length="2mm", filename="registration_ticks.svg"):
    # Force lowercase to prevent 'A1' vs 'a1' standard size errors
    paper_size = paper_size.lower()
    
    # vpype translates the string (whether standard or custom) into pixel values
    w, h = vpype.convert_page_size(paper_size)
    t = vpype.convert_length(tick_length)
    
    # Draw a single diagonal line from each corner pointing inward
    commands = [
        f"line 0 0 {t} {t}",                 # Top-Left
        f"line {w} 0 {w-t} {t}",             # Top-Right
        f"line {w} {h} {w-t} {h-t}",         # Bottom-Right
        f"line 0 {h} {t} {h-t}"              # Bottom-Left
    ]
    
    # Build and execute the vpype command
    vpype_cmd = ["vpype"] + " ".join(commands).split() + ["write", "--page-size", paper_size, filename]
    subprocess.run(vpype_cmd)
    
    print(f"Saved '{filename}' at size {paper_size} with {tick_length} diagonal corner ticks.")

def print_paper_sizes():
    guide = """
    ================================================================================
                               PLOTTER PAPER & CARD SIZES
    ================================================================================
    STANDARD SIZES (Built-in to vpype - Use Case-Insensitive Name)
    --------------------------------------------------------------------------------
    "a1"       -->  594.0 x 841.0 mm  |  23.4 x 33.1 in   (Standard A1 Blueprint)
    "a2"       -->  420.0 x 594.0 mm  |  16.5 x 23.4 in   (A2 Art Print)
    "a3"       -->  297.0 x 420.0 mm  |  11.7 x 16.5 in   (Medium Sketch)
    "a4"       -->  210.0 x 297.0 mm  |   8.3 x 11.7 in   (A4 Document)
    "letter"   -->  215.9 x 279.4 mm  |   8.5 x 11.0 in   (Standard Document)
    "legal"    -->  215.9 x 355.6 mm  |   8.5 x 14.0 in   (Legal Document)
    "tabloid"  -->  279.4 x 431.8 mm  |  11.0 x 17.0 in   (Tabloid/Ledger)
    "executive"-->  184.1 x 266.7 mm  |  7.25 x 10.5 in   (Smaller Notebook)

    POSTCARDS (Custom Sizes - Must Use "WIDTHxHEIGHT[unit]" Format)
    --------------------------------------------------------------------------------
    "4x6in"    -->  101.6 x 152.4 mm  |   4.0 x  6.0 in   (Standard US Postcard)
    "5x7in"    -->  127.0 x 177.8 mm  |   5.0 x  7.0 in   (Large US Postcard)
    "a6"       -->  105.0 x 148.0 mm  |   4.1 x  5.8 in   (ISO Postcard - Built-in Name)

    ENVELOPES (Custom Sizes - Must Use "WIDTHxHEIGHT[unit]" Format)
    --------------------------------------------------------------------------------
    "4.125x9.5in"   -->  104.8 x 241.3 mm  |  4.125 x 9.5  in  (US #10 Business)
    "4.375x5.75in"  -->  111.1 x 146.1 mm  |  4.375 x 5.75 in  (US A2 Invitation)
    "4.75x6.5in"    -->  120.7 x 165.1 mm  |   4.75 x 6.5  in  (US A6 / Cards)
    "5.25x7.25in"   -->  133.4 x 184.2 mm  |   5.25 x 7.25 in  (US A7 / Fits 5x7)
    "110x220mm"     -->  110.0 x 220.0 mm  |   4.33 x 8.66 in  (ISO DL Pocket)
    "114x162mm"     -->  114.0 x 162.0 mm  |   4.49 x 6.38 in  (ISO C6 Pocket)
    ================================================================================
    To draw corner ticks on any size above:
    >> pu.generate_registration_ticks("a1", "2mm")
    >> pu.generate_registration_ticks("4x6in", "0.1in", filename="postcard_ticks.svg")
    ================================================================================
    """
    print(guide)