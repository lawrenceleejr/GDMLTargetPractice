cmake_minimum_required(VERSION 3.15)
project(g4sim)

# Geant4 11.x requires C++17; pin it (the ntuple code uses structured bindings).
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# --------------------
# Find ROOT
# --------------------
find_package(ROOT REQUIRED COMPONENTS RIO Tree Hist)
include_directories(${ROOT_INCLUDE_DIRS})

# --------------------
# Find Geant4
# --------------------
find_package(Geant4 REQUIRED ui_all vis_all)
include(${Geant4_USE_FILE})

# --------------------
# Find HepMC3 (the standard generator->Geant4 interchange the hand-off uses)
# --------------------
find_package(HepMC3 REQUIRED)

# --------------------
# Optional: Celeritas (offload e-/e+/gamma transport to GPU or CPU)
# --------------------
option(WITH_CELERITAS "Enable Celeritas offload of EM tracks" OFF)
if(WITH_CELERITAS)
  find_package(Celeritas 0.6 REQUIRED)
  if(NOT CELERITAS_USE_Geant4)
    message(FATAL_ERROR "WITH_CELERITAS requires a Celeritas installation built with CELERITAS_USE_Geant4=ON")
  endif()
endif()

# --------------------
# Sources
# --------------------
set(SOURCES
    BSMPhysics.cc
    Biasing.cc
    BiasingMessenger.cc
    DetectorConstruction.cc
    DetectorMessenger.cc
    PrimaryGenerator.cc
    PrimaryGeneratorMessenger.cc
    RunAction.cc
    RunActionMessenger.cc
    EventAction.cc
    SplitNeutrinoPhysics.cc
    SteppingAction.cc
    ExitWriter.cc
    main.cc
)

# --------------------
# Executable
# --------------------
add_executable(g4sim ${SOURCES})
add_compile_options(-Wno-shadow)
target_compile_options(g4sim PRIVATE -Wno-shadow)

# Link libraries. ROOT is linked via its imported targets, which carry the
# ROOT include directories (ROOT_INCLUDE_DIRS is not reliably populated by
# ROOT 6.26's CMake config under newer CMake versions).
# HepMC3 exports either the modern imported target HepMC3::HepMC3 or the
# ${HEPMC3_LIBRARIES} variable, depending on version -- prefer the target.
if(TARGET HepMC3::HepMC3)
  set(HEPMC3_LINK HepMC3::HepMC3)
else()
  set(HEPMC3_LINK ${HEPMC3_LIBRARIES})
  include_directories(${HEPMC3_INCLUDE_DIR})
endif()

if(WITH_CELERITAS)
  target_compile_definitions(g4sim PRIVATE USE_CELERITAS)
  celeritas_target_link_libraries(g4sim ROOT::RIO ROOT::Tree ROOT::Hist ${Geant4_LIBRARIES} ${HEPMC3_LINK} Celeritas::G4)
else()
  target_link_libraries(g4sim ROOT::RIO ROOT::Tree ROOT::Hist ${Geant4_LIBRARIES} ${HEPMC3_LINK})
endif()
