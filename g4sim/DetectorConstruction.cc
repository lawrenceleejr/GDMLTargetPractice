#include "DetectorConstruction.hh"
#include "DetectorMessenger.hh"

#include "G4Exception.hh"
#include "G4RunManager.hh"
#include "G4GDMLParser.hh"
#include "G4SystemOfUnits.hh"
#include "G4Region.hh"
#include "G4LogicalVolumeStore.hh"
#include "G4UserLimits.hh"
#include "G4NistManager.hh"
#include "G4UniformMagField.hh"
#include "G4FieldManager.hh"
#include "G4TransportationManager.hh"
#include "G4Box.hh"
#include "G4LogicalVolume.hh"
#include "G4PVPlacement.hh"
#include "G4ThreeVector.hh"


DetectorConstruction::DetectorConstruction()
{
    fMessenger = new DetectorMessenger(this);

    // Build a minimal air-filled world so Construct() always has a valid
    // world volume even before any GDML file is loaded via /detector/readGDML.
    G4Material* air = G4NistManager::Instance()->FindOrBuildMaterial("G4_AIR");
    G4Box*           worldSolid = new G4Box("DefaultWorld", 5.*m, 5.*m, 5.*m);
    G4LogicalVolume* worldLV    = new G4LogicalVolume(worldSolid, air, "DefaultWorld");
    fWorld = new G4PVPlacement(nullptr, G4ThreeVector(), worldLV,
                               "DefaultWorld", nullptr, false, 0);

    G4cout << "DetectorConstruction: default stub world created. "
           << "Use /detector/readGDML <file> in your macro to load a geometry."
           << G4endl;
}

DetectorConstruction::~DetectorConstruction()
{
    delete fMessenger;
}

// Allows macro-based GDML reloading
void DetectorConstruction::ReadGDML(const G4String& filename)
{
    G4cout << "Reading GDML file: " << filename << G4endl;

    fParser.Read(filename, false);  // false disables schema validation

    fWorld = fParser.GetWorldVolume();
    if (!fWorld) {
        G4cerr << "GDML read, but world volume is NULL!" << G4endl;
        G4cerr << "Check that your GDML defines a <world> and all solids/materials." << G4endl;

        G4Exception("DetectorConstruction::ReadGDML",
                    "NoGDML",
                    FatalException,
                    "World volume is NULL after reading GDML.");
    }

    G4cout << "GDML loaded successfully. World volume: "
           << fWorld->GetName() << G4endl;

    // Reinitialize geometry after macro reload
    G4RunManager::GetRunManager()->ReinitializeGeometry();
}

// Set (or clear, with a zero vector) a uniform magnetic field over the
// whole geometry, e.g. a solenoid channel along z for muon capture.
void DetectorConstruction::SetGlobalField(const G4ThreeVector& fieldValue)
{
    auto* fieldMgr =
        G4TransportationManager::GetTransportationManager()->GetFieldManager();

    G4MagneticField* oldField = fGlobalField;
    if (fieldValue.mag2() > 0.) {
        fGlobalField = new G4UniformMagField(fieldValue);
        fieldMgr->SetDetectorField(fGlobalField);
        fieldMgr->CreateChordFinder(static_cast<G4MagneticField*>(fGlobalField));
        G4cout << "Global uniform magnetic field set: "
               << fieldValue / tesla << " T" << G4endl;
#ifdef USE_CELERITAS
        G4cout << "WARNING: the Celeritas offload in this build assumes zero "
                  "magnetic field for e-/e+/gamma transport.\n"
                  "Set CELER_DISABLE=1 when running with a field." << G4endl;
#endif
    } else {
        fGlobalField = nullptr;
        fieldMgr->SetDetectorField(nullptr);
        G4cout << "Global magnetic field disabled." << G4endl;
    }
    delete oldField;
}

G4VPhysicalVolume* DetectorConstruction::Construct()
{
    if (!fWorld) {
        G4Exception("DetectorConstruction::Construct()",
                    "NoGDML",
                    FatalException,
                    "World volume is NULL. Call /detector/readGDML before /run/initialize.");
    }

    G4LogicalVolume* worldLogical = fWorld->GetLogicalVolume();
    if (worldLogical) {
             G4double minStep = 1.0*mm;  // you can reduce if needed
      G4UserLimits* stepLimits = new G4UserLimits(minStep);
      worldLogical->SetUserLimits(stepLimits);
      G4double maxStep = 10*cm;  // limit max step to 10 cm
      worldLogical->SetUserLimits(new G4UserLimits(maxStep));
 
      G4cout << "Minimum step size set for world: " << minStep/mm << " mm" << G4endl;
    } else {
        G4cerr << "World logical volume not found!" << G4endl;
    }
    
    // -----------------------------------------
    // Create neutrino target region
    // -----------------------------------------
    //auto targetRegion = new G4Region("target");
    fTargetRegion = new G4Region("target");

    auto lvStore = G4LogicalVolumeStore::GetInstance();

    G4cout << "\n=== Assigning Neutrino Region ===" << G4endl;

    for (auto lv : *lvStore) {
        // Skip the World volume
        if (lv == fWorldLogical) continue;

        // Skip Air, Vacuum, or Galactic volumes
        if (lv->GetMaterial()) {
            G4String matName = lv->GetMaterial()->GetName();
            if (matName.find("Air") != std::string::npos || 
                matName.find("Vacuum") != std::string::npos ||
                matName.find("vacuum") != std::string::npos ||
                matName.find("Galactic") != std::string::npos) {
                continue; 
            }
        }

        // Add everything else to the target region
        fTargetRegion->AddRootLogicalVolume(lv);
        fTargetVolumes.push_back(lv);
        G4cout << ">>> Neutrino target set on: " << lv->GetName() << G4endl;
    }

    G4cout << "===============================\n" << G4endl;
    

    return fWorld;
}
