# -*- coding: utf-8 -*-
"""Países, regiones y ciudades que se pueden elegir al registrarse o enviar un pedido.

Estructura: {país: {región: [ciudades principales]}}
  - Región con lista de ciudades -> la tienda ofrece elegirla (o escribir otra: la lista es parcial).
  - País con regiones vacías ({}) -> la región se escribe a mano.
  - Región con lista vacía ([])  -> la ciudad se escribe a mano.
El servidor valida país y región con estas listas; la ciudad solo se revisa que sea texto razonable.
"""

COLOMBIA = {
    "Amazonas": ["Leticia", "Puerto Nariño"],
    "Antioquia": ["Medellín", "Bello", "Itagüí", "Envigado", "Sabaneta", "La Estrella", "Caldas", "Copacabana",
                  "Girardota", "Rionegro", "Marinilla", "La Ceja", "El Retiro", "Guatapé", "Apartadó", "Carepa",
                  "Chigorodó", "Turbo", "Necoclí", "Caucasia", "Yarumal", "Andes", "Jericó", "Santa Fe de Antioquia"],
    "Arauca": ["Arauca", "Arauquita", "Saravena", "Tame", "Fortul", "Cravo Norte"],
    "Atlántico": ["Barranquilla", "Soledad", "Malambo", "Puerto Colombia", "Galapa", "Baranoa", "Sabanalarga",
                  "Sabanagrande", "Santo Tomás", "Palmar de Varela", "Luruaco"],
    "Bogotá D.C.": ["Bogotá"],
    "Bolívar": ["Cartagena de Indias", "Magangué", "Turbaco", "Arjona", "El Carmen de Bolívar", "Santa Cruz de Mompox",
                "San Juan Nepomuceno", "María la Baja", "Simití", "Calamar"],
    "Boyacá": ["Tunja", "Duitama", "Sogamoso", "Chiquinquirá", "Paipa", "Puerto Boyacá", "Villa de Leyva",
               "Moniquirá", "Samacá", "Garagoa"],
    "Caldas": ["Manizales", "Villamaría", "La Dorada", "Chinchiná", "Riosucio", "Anserma", "Aguadas", "Salamina",
               "Supía", "Neira"],
    "Caquetá": ["Florencia", "San Vicente del Caguán", "Puerto Rico", "Cartagena del Chairá",
                "Belén de los Andaquíes", "El Doncello", "Morelia"],
    "Casanare": ["Yopal", "Aguazul", "Villanueva", "Paz de Ariporo", "Tauramena", "Monterrey", "Maní"],
    "Cauca": ["Popayán", "Santander de Quilichao", "Puerto Tejada", "Patía", "Miranda", "Silvia", "Timbío",
              "Piendamó", "Guapi", "Corinto"],
    "Cesar": ["Valledupar", "Aguachica", "Agustín Codazzi", "Bosconia", "Chimichagua", "La Jagua de Ibirico",
              "Curumaní", "Pailitas", "San Alberto"],
    "Chocó": ["Quibdó", "Istmina", "Tadó", "Acandí", "Bahía Solano", "Condoto", "Nuquí", "Riosucio"],
    "Córdoba": ["Montería", "Lorica", "Cereté", "Sahagún", "Montelíbano", "Planeta Rica", "Tierralta", "Ayapel",
                "Puerto Escondido", "San Andrés de Sotavento", "Chinú", "San Pelayo", "Ciénaga de Oro", "Buenavista",
                "La Apartada", "Pueblo Nuevo", "Puerto Libertador", "San Antero", "Moñitos", "Canalete",
                "Los Córdobas", "Valencia", "San Bernardo del Viento", "Momil", "Purísima", "Tuchín", "Chimá",
                "Cotorra", "San Carlos", "San José de Uré"],
    "Cundinamarca": ["Soacha", "Fusagasugá", "Chía", "Zipaquirá", "Facatativá", "Girardot", "Cajicá", "Mosquera",
                     "Madrid", "Funza", "Sibaté", "La Calera", "Cota", "Tocancipá", "Villeta", "Ubaté"],
    "Guainía": ["Inírida"],
    "Guaviare": ["San José del Guaviare", "Calamar", "El Retorno", "Miraflores"],
    "Huila": ["Neiva", "Pitalito", "Garzón", "La Plata", "Campoalegre", "Gigante", "Rivera", "Aipe", "San Agustín"],
    "La Guajira": ["Riohacha", "Maicao", "Uribia", "Fonseca", "San Juan del Cesar", "Villanueva", "Dibulla",
                   "Albania", "Barrancas", "Manaure"],
    "Magdalena": ["Santa Marta", "Ciénaga", "Fundación", "El Banco", "Plato", "Aracataca", "Zona Bananera",
                  "Pivijay", "Santa Ana", "Guamal"],
    "Meta": ["Villavicencio", "Acacías", "Granada", "Puerto López", "Restrepo", "Cumaral", "San Martín",
             "Puerto Gaitán"],
    "Nariño": ["Pasto", "Tumaco", "Ipiales", "Túquerres", "La Unión", "Samaniego", "Sandoná", "Barbacoas"],
    "Norte de Santander": ["Cúcuta", "Ocaña", "Pamplona", "Villa del Rosario", "Los Patios", "Tibú", "El Zulia",
                           "Ábrego"],
    "Putumayo": ["Mocoa", "Puerto Asís", "Orito", "Valle del Guamuez", "Sibundoy", "Villagarzón",
                 "Puerto Leguízamo"],
    "Quindío": ["Armenia", "Calarcá", "Montenegro", "La Tebaida", "Quimbaya", "Circasia", "Salento", "Filandia"],
    "Risaralda": ["Pereira", "Dosquebradas", "La Virginia", "Santa Rosa de Cabal", "Marsella", "Belén de Umbría",
                  "Quinchía"],
    "San Andrés y Providencia": ["San Andrés", "Providencia"],
    "Santander": ["Bucaramanga", "Floridablanca", "Girón", "Piedecuesta", "Barrancabermeja", "San Gil", "Socorro",
                  "Barbosa", "Málaga", "Vélez", "Lebrija"],
    "Sucre": ["Sincelejo", "Corozal", "Sampués", "San Marcos", "Tolú", "Coveñas", "Sincé", "San Onofre", "Majagual"],
    "Tolima": ["Ibagué", "Espinal", "Honda", "Melgar", "Chaparral", "Líbano", "Mariquita", "Flandes", "Guamo",
               "Ortega"],
    "Valle del Cauca": ["Cali", "Palmira", "Buenaventura", "Tuluá", "Cartago", "Buga", "Jamundí", "Yumbo",
                        "Candelaria", "Florida", "Sevilla", "Roldanillo", "Zarzal"],
    "Vaupés": ["Mitú"],
    "Vichada": ["Puerto Carreño", "La Primavera", "Cumaribo", "Santa Rosalía"],
}

ESTADOS_UNIDOS = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

UBICACIONES = {
    "Colombia": COLOMBIA,
    "Estados Unidos": {estado: [] for estado in ESTADOS_UNIDOS},
    # Países con región y ciudad escritas a mano
    "Argentina": {}, "Brasil": {}, "Chile": {}, "Ecuador": {}, "España": {}, "México": {},
    "Panamá": {}, "Perú": {}, "Venezuela": {},
}

# Formato del código postal por país (los demás aceptan de 3 a 10 letras, números, espacios o guiones)
CODIGO_POSTAL = {"Colombia": r"\d{6}", "Estados Unidos": r"\d{5}(-\d{4})?"}
CODIGO_POSTAL_GENERAL = r"[A-Za-z0-9][A-Za-z0-9 -]{1,8}[A-Za-z0-9]"


def como_lista():
    """Forma que recibe la tienda: [{pais, regiones: [{nombre, ciudades}]}], Colombia primero."""
    return [{"pais": pais, "regiones": [{"nombre": r, "ciudades": c} for r, c in regiones.items()]}
            for pais, regiones in UBICACIONES.items()]
