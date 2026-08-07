cmake_minimum_required(VERSION 3.20)
project(BombermanPartyEditionRecomp C CXX)

set(CMAKE_C_STANDARD 99)
set(CMAKE_CXX_STANDARD 17)

# Profile-guided optimization for intro + OPENING.STR FMV host path.
#   cmake -DPSX_PGO=generate ... && ninja && run intro (~2–3 min)
#   cmake -DPSX_PGO=use ... && ninja
# Profiles land in ${CMAKE_BINARY_DIR}/pgo/ (see scripts/pgo_bpe_intro.sh).
set(PSX_PGO "" CACHE STRING "PGO mode: empty | generate | use")
set(PSX_PGO_DIR "${CMAKE_BINARY_DIR}/pgo" CACHE PATH "PGO profile directory")
if(PSX_PGO STREQUAL "generate")
    file(MAKE_DIRECTORY "${PSX_PGO_DIR}")
    if(CMAKE_C_COMPILER_ID MATCHES "Clang")
        add_compile_options(-fprofile-instr-generate)
        add_link_options(-fprofile-instr-generate)
    else()
        add_compile_options(-fprofile-generate=${PSX_PGO_DIR})
        add_link_options(-fprofile-generate=${PSX_PGO_DIR})
    endif()
elseif(PSX_PGO STREQUAL "use")
    if(CMAKE_C_COMPILER_ID MATCHES "Clang")
        set(_psx_pgo_profdata "${PSX_PGO_DIR}/default.profdata")
        if(NOT EXISTS "${_psx_pgo_profdata}")
            message(FATAL_ERROR
                "PSX_PGO=use (Clang): missing ${_psx_pgo_profdata}. "
                "Train with LLVM_PROFILE_FILE=.../pgo/bpe-%p.profraw then "
                "llvm-profdata merge -o default.profdata *.profraw")
        endif()
        add_compile_options("-fprofile-instr-use=${_psx_pgo_profdata}")
        add_link_options("-fprofile-instr-use=${_psx_pgo_profdata}")
    else()
        add_compile_options(
            -fprofile-use=${PSX_PGO_DIR}
            -fprofile-correction
            -Wno-coverage-mismatch
            -Wno-missing-profile
        )
        add_link_options(-fprofile-use=${PSX_PGO_DIR})
    endif()
endif()

# Legacy aliases still accepted by CI / docs.
option(BPE_FORCE_SETUP_HOST
    "Build without linking game C even if generated/ exists" OFF)
option(BPE_REQUIRE_GAME_C
    "Fail configure if generated game C is missing (old CI behavior)" OFF)

set(PSXRECOMP_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/psxrecomp" CACHE PATH
    "Path to psxrecomp")
# Opt in before include(runtime.cmake) so recomp-net is discovered/linked at
# parse time (ENABLE_NETPLAY_IF_PRESENT alone is too late — option(PSX_NETPLAY)
# already ran OFF).
if(EXISTS "${PSXRECOMP_ROOT}/lib/recomp-net/CMakeLists.txt")
    set(PSX_NETPLAY ON CACHE BOOL
        "Link recomp-net delay-sync (opt-in; needs recomp-net)" FORCE)
endif()
# Self-build first-run wizard + Generate & rebuild (required for setup-host CI).
set(PSX_SETUP_WIZARD ON CACHE BOOL
    "Advertise first-run setup wizard + Generate & rebuild in recomp-ui" FORCE)
include("${PSXRECOMP_ROOT}/runtime/runtime.cmake")

psxrecomp_add_game_runtime(psx-runtime
    ENABLE_NETPLAY_IF_PRESENT
    ENABLE_SETUP_WIZARD
    WINDOW_TITLE "Bomberman Party Edition Recompiled"
    DEFAULT_GAME_CONFIG_PATH "game.toml"
    LAUNCHER_BOXART "${CMAKE_CURRENT_SOURCE_DIR}/launcher_assets/img/boxart.tga"
    DEBUG_PORT 4530
    MAX_PLAYERS 5
    GEN_MARKER "generated/SLUS_011.89_dispatch.c"
    GEN_FULL_GLOB "generated/SLUS_011.89_full_*.c"
    GEN_FULL_FALLBACK "generated/SLUS_011.89_full.c"
    CODEGEN_SETUP_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/codegen_setup.c"
)
