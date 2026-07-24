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

# Release version pin for netplay lobby matching (baked as PSX_GAME_VERSION).
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" _BPE_VER_RAW)
string(STRIP "${_BPE_VER_RAW}" BPE_RELEASE_VERSION)
set(PSX_GAME_VERSION "" CACHE STRING
    "Lobby release pin (empty = Release uses VERSION file, else dev)")
if(PSX_GAME_VERSION STREQUAL "")
    get_property(_bpe_is_multi GLOBAL PROPERTY GENERATOR_IS_MULTI_CONFIG)
    if(_bpe_is_multi)
        set(PSX_GAME_VERSION "$<IF:$<CONFIG:Release>,${BPE_RELEASE_VERSION},dev>")
    elseif(CMAKE_BUILD_TYPE STREQUAL "Release")
        set(PSX_GAME_VERSION "${BPE_RELEASE_VERSION}")
    else()
        set(PSX_GAME_VERSION "dev")
    endif()
endif()
message(STATUS "BPE lobby game_version: ${PSX_GAME_VERSION} (from VERSION=${BPE_RELEASE_VERSION})")

set(PSXRECOMP_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/psxrecomp" CACHE PATH
    "Path to psxrecomp")

# Game-root recomp-ui (not under psxrecomp/lib/).
set(RECOMP_UI_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/recomp-ui" CACHE PATH
    "Path to recomp-ui launcher" FORCE)

# Prefer the framework-vendored recomp-net submodule (CI recursive checkout).
if(NOT RECOMP_NET_ROOT AND EXISTS "${PSXRECOMP_ROOT}/lib/recomp-net/CMakeLists.txt")
    set(RECOMP_NET_ROOT "${PSXRECOMP_ROOT}/lib/recomp-net" CACHE PATH
        "Path to recomp-net; empty = auto-discover" FORCE)
endif()

include("${PSXRECOMP_ROOT}/runtime/runtime.cmake")

# Split-TU build: pick up sharded full_*.c if present, else the monolithic full.c.
file(GLOB BPE_GENERATED_FULL_SHARDS CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/generated/SLUS_011.89_full_*.c")
if(BPE_GENERATED_FULL_SHARDS)
    set(BPE_GENERATED_FULL "${BPE_GENERATED_FULL_SHARDS}")
else()
    set(BPE_GENERATED_FULL
        "${CMAKE_CURRENT_SOURCE_DIR}/generated/SLUS_011.89_full.c")
endif()
set(BPE_GENERATED_DISPATCH
    "${CMAKE_CURRENT_SOURCE_DIR}/generated/SLUS_011.89_dispatch.c")

psxrecomp_add_runtime_target(psx-runtime
    GAME_GENERATED_FULL_C "${BPE_GENERATED_FULL}"
    GAME_GENERATED_DISPATCH_C "${BPE_GENERATED_DISPATCH}"
    DEBUG_PORT 4530
    WINDOW_TITLE "Bomberman Party Edition Recompiled"
    DEFAULT_GAME_CONFIG_PATH "game.toml"
    LAUNCHER_BOXART "${CMAKE_CURRENT_SOURCE_DIR}/launcher_assets/img/boxart.tga"
    GAME_VERSION "${PSX_GAME_VERSION}"
    MAX_PLAYERS 5
)
