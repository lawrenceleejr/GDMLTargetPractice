#ifndef DetectorConstruction_h
#define DetectorConstruction_h 1

#include "G4VUserDetectorConstruction.hh"
#include "G4GDMLParser.hh"
#include "G4VPhysicalVolume.hh"
#include "G4MagneticField.hh"
#include "G4ThreeVector.hh"

class DetectorMessenger;   // Forward declaration

class DetectorConstruction : public G4VUserDetectorConstruction {
public:
    std::vector<G4LogicalVolume*> fTargetVolumes;
    const std::vector<G4LogicalVolume*>& GetTargetVolumes() const
{
    return fTargetVolumes;
}
    DetectorConstruction();
    virtual ~DetectorConstruction();

    virtual G4VPhysicalVolume* Construct();
    
    void ConstructSDandField() override;
    // Adding this line to allow worker threads to bias neutrino interactions

    void ReadGDML(const G4String& filename);
    G4Region* GetTargetRegion() const { return fTargetRegion; }
    void SetGlobalField(const G4ThreeVector& fieldValue);

private:
    G4GDMLParser       fParser;
    G4VPhysicalVolume* fWorld = nullptr;
    G4Region* fTargetRegion = nullptr;
    DetectorMessenger* fMessenger;
    G4MagneticField*   fGlobalField = nullptr;
    G4LogicalVolume* fWorldLogical = nullptr;
};

#endif
